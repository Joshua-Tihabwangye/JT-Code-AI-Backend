"""Phase 13 exit proofs: race-condition and overspend safety of metering, plus
quotas, concurrency limits, the immutable ledger, rate limits, reconciliation
and the usage dashboards."""

from __future__ import annotations

import threading
from decimal import Decimal

import pytest
from django.db import DatabaseError, connection, transaction
from django.utils import timezone
from rest_framework.test import APIClient

from apps.billing.models import CreditLedger, CreditWallet, Entitlement, Plan
from apps.identity.models import Organization, Role, UserOrganization, UserRole
from apps.usage import services as metering
from apps.usage.exceptions import (
    ConcurrencyLimitExceeded,
    InsufficientCredits,
    QuotaExceeded,
    SpendingLimitReached,
)
from apps.usage.models import Feature, UsageReconciliation, UsageRecord, UsageReservation
from apps.usage.pricing import credits_for_cost


def make_org(django_user_model, label: str, *, balance: str = "1000"):
    owner = django_user_model.objects.create_user(
        username=f"{label}-owner", supabase_user_id=f"supabase-{label}", email=f"{label}@example.test"
    )
    organization = Organization.objects.create(name=f"{label} org", slug=f"{label}-org", owner=owner)
    UserOrganization.objects.create(user=owner, organization=organization)
    role, _ = Role.objects.get_or_create(name=Role.RoleType.ADMIN)
    UserRole.objects.get_or_create(user=owner, role=role, organization=organization)
    CreditWallet.objects.update_or_create(organization=organization, defaults={"balance": Decimal(balance)})
    return organization, owner


def run_concurrently(count: int, work) -> list:
    """Run ``work(i)`` in ``count`` threads released together; collect results/exceptions."""
    barrier = threading.Barrier(count)
    results: list = [None] * count

    def target(index: int) -> None:
        try:
            barrier.wait()
            results[index] = work(index)
        except Exception as exc:  # noqa: BLE001 - collected for assertions
            results[index] = exc
        finally:
            connection.close()

    threads = [threading.Thread(target=target, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    return results


# --- Exit criteria: races and overspend ------------------------------------------


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_concurrent_reservations_cannot_overspend_the_wallet(django_user_model):
    organization, owner = make_org(django_user_model, "race-wallet", balance="100")

    def work(index: int):
        return metering.reserve(
            organization=organization,
            user=owner,
            feature=Feature.CHAT_MESSAGES,
            source_type="race",
            source_id=f"wallet-{index}",
            credits=Decimal("20"),
        )

    results = run_concurrently(10, work)
    accepted = [result for result in results if isinstance(result, UsageReservation)]
    refused = [result for result in results if isinstance(result, InsufficientCredits)]
    assert len(accepted) == 5 and len(refused) == 5, results
    wallet = CreditWallet.objects.get(organization=organization)
    assert wallet.reserved_balance == Decimal("100") and wallet.available_balance == 0


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_concurrent_reservations_cannot_exceed_a_hard_quota(django_user_model):
    organization, owner = make_org(django_user_model, "race-quota")
    plan = Plan.objects.create(name="Quota", slug="race-quota-plan", price_cents=0)
    Entitlement.objects.create(
        plan=plan, feature=Feature.CHAT_MESSAGES, limit_type=Entitlement.LimitType.HARD, limit_value=3
    )
    from apps.billing.models import Subscription

    Subscription.objects.create(
        organization=organization,
        plan=plan,
        status=Subscription.Status.ACTIVE,
        provider_subscription_id="sub_race_quota",
        current_period_start=timezone.now(),
        current_period_end=timezone.now() + timezone.timedelta(days=30),
    )

    def work(index: int):
        return metering.reserve(
            organization=organization,
            user=owner,
            feature=Feature.CHAT_MESSAGES,
            source_type="race",
            source_id=f"quota-{index}",
        )

    results = run_concurrently(8, work)
    assert sum(isinstance(result, UsageReservation) for result in results) == 3
    assert sum(isinstance(result, QuotaExceeded) for result in results) == 5


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_concurrent_job_creation_respects_the_concurrency_limit(django_user_model, settings):
    from apps.jobs.models import Job
    from apps.jobs.services import reserve_job_credits

    settings.MAX_CONCURRENT_JOBS_PER_TENANT = 2
    organization, owner = make_org(django_user_model, "race-jobs")

    def work(index: int):
        with transaction.atomic():
            job = Job.objects.create(
                owner=owner,
                organization=organization,
                task_type=Job.TaskType.GENERAL_QUESTION,
                idempotency_key=f"race-{index}",
                input_payload={},
            )
            reserve_job_credits(job, owner)
            return job

    results = run_concurrently(6, work)
    assert sum(isinstance(result, Job) for result in results) == 2
    assert sum(isinstance(result, ConcurrencyLimitExceeded) for result in results) == 4
    assert Job.objects.filter(organization=organization).count() == 2


@pytest.mark.django_db
def test_settlement_never_charges_more_than_reserved_or_the_balance(django_user_model):
    organization, owner = make_org(django_user_model, "overspend", balance="50")
    reservation = metering.reserve(
        organization=organization,
        user=owner,
        feature=Feature.CHAT_MESSAGES,
        source_type="test",
        source_id="overspend",
        credits=Decimal("10"),
    )
    record = metering.settle(reservation.id, cost=metering.UsageCost(provider_cost_usd=Decimal("5")))
    assert record.credits_charged == Decimal("10")
    assert record.credits_uncollected == credits_for_cost(Decimal("5")) - Decimal("10")
    wallet = CreditWallet.objects.get(organization=organization)
    assert wallet.balance == Decimal("40") and wallet.reserved_balance == 0
    again = metering.settle(reservation.id, cost=metering.UsageCost(provider_cost_usd=Decimal("5")))
    assert again.id == record.id  # idempotent
    assert CreditWallet.objects.get(organization=organization).balance == Decimal("40")


@pytest.mark.django_db
def test_settlement_uses_provider_cost_with_fx_margin_and_minimum(django_user_model, settings):
    settings.BILLING_CREDIT_VALUE_USD = 0.01
    settings.BILLING_FX_BUFFER = 1.05
    settings.BILLING_MARGIN_MULTIPLIER = 1.25
    organization, owner = make_org(django_user_model, "pricing")
    held = metering.reserve(
        organization=organization, user=owner, feature=Feature.RAG_QUERIES, source_type="t", source_id="cost"
    )
    record = metering.settle(
        held.id, cost=metering.UsageCost(provider_cost_usd=Decimal("0.08"), input_tokens=10)
    )
    assert record.basis == UsageRecord.Basis.PROVIDER_COST
    assert record.credits_charged == Decimal("10.500000")  # 0.08 * 1.05 * 1.25 / 0.01
    tiny = metering.reserve(
        organization=organization, user=owner, feature=Feature.RAG_QUERIES, source_type="t", source_id="tiny"
    )
    assert metering.settle(
        tiny.id, cost=metering.UsageCost(provider_cost_usd=Decimal("0.000001"))
    ).credits_charged == (
        Decimal("2")  # the feature's flat minimum
    )
    ledger = CreditLedger.objects.filter(wallet__organization=organization, reason="usage_rag")
    assert (
        ledger.count() == 2
        and not CreditLedger.objects.filter(idempotency_key__startswith="reserve_").exists()
    )


@pytest.mark.django_db
def test_monthly_spending_limit_blocks_new_reservations(django_user_model):
    organization, owner = make_org(django_user_model, "spend")
    CreditWallet.objects.filter(organization=organization).update(monthly_spending_limit=Decimal("15"))
    metering.reserve(
        organization=organization, user=owner, feature=Feature.CHAT_MESSAGES, source_type="t", source_id="a"
    )
    with pytest.raises(SpendingLimitReached):
        metering.reserve(
            organization=organization,
            user=owner,
            feature=Feature.CHAT_MESSAGES,
            source_type="t",
            source_id="b",
        )


# --- Immutable ledger ---------------------------------------------------------------


@pytest.mark.django_db
def test_usage_records_and_ledger_are_append_only_but_tenant_deletion_purges(django_user_model):
    organization, owner = make_org(django_user_model, "immutable")
    reservation = metering.reserve(
        organization=organization, user=owner, feature=Feature.API_CALLS, source_type="t", source_id="x"
    )
    record = metering.settle(reservation.id)
    ledger = CreditLedger.objects.filter(wallet__organization=organization).first()
    for statement in (
        lambda: UsageRecord.objects.filter(id=record.id).update(credits_charged=0),
        lambda: UsageRecord.objects.filter(id=record.id).delete(),
        lambda: CreditLedger.objects.filter(id=ledger.id).update(credits=0),
        lambda: CreditLedger.objects.filter(id=ledger.id).delete(),
    ):
        with pytest.raises(DatabaseError), transaction.atomic():
            statement()
    owner.delete()  # clearing the nullable user link is allowed
    record.refresh_from_db()
    assert record.user_id is None
    Organization.objects.get(id=organization.id).delete()
    assert not UsageRecord.objects.filter(id=record.id).exists()


# --- Settlement lifecycle -------------------------------------------------------------


@pytest.mark.django_db
def test_job_lifecycle_reserves_then_settles_from_model_runs(django_user_model):
    from apps.ai_gateway.models import Model, ModelRun
    from apps.jobs.models import Job
    from apps.jobs.services import reserve_job_credits
    from apps.jobs.transitions import apply_status_update

    organization, owner = make_org(django_user_model, "joblife")
    job = Job.objects.create(
        owner=owner,
        organization=organization,
        task_type=Job.TaskType.RAG_QUERY,
        idempotency_key="joblife",
        input_payload={},
    )
    reserve_job_credits(job, owner)
    assert job.reserved_credits == Decimal("25")
    model = Model.objects.select_related("provider").first()
    ModelRun.objects.create(
        request_id=job.request_id,
        job_id=job.id,
        provider=model.provider,
        model=model,
        organization=organization,
        status=ModelRun.Status.COMPLETED,
        provider_cost_usd=Decimal("0.04"),
        input_tokens=100,
        output_tokens=50,
    )
    apply_status_update(job.id, {"status": Job.Status.COMPLETED, "result": {}})
    job.refresh_from_db()
    record = UsageRecord.objects.get(source_type="job", source_id=str(job.id))
    assert record.feature == Feature.RAG_QUERIES and record.input_tokens == 100
    assert job.actual_credits == record.credits_charged == credits_for_cost(Decimal("0.04"))


@pytest.mark.django_db
def test_failed_sources_release_and_sweep_settles_or_expires_holds(django_user_model):
    from apps.jobs.models import Job
    from apps.usage.tasks import settle_finished_reservations

    organization, owner = make_org(django_user_model, "sweep")
    failed = Job.objects.create(
        owner=owner,
        organization=organization,
        task_type=Job.TaskType.GENERAL_QUESTION,
        idempotency_key="sweep-failed",
        input_payload={},
        status=Job.Status.FAILED,
    )
    held = metering.reserve(
        organization=organization,
        user=owner,
        feature=Feature.CHAT_MESSAGES,
        source_type="job",
        source_id=failed.id,
    )
    orphan = metering.reserve(
        organization=organization,
        user=owner,
        feature=Feature.CHAT_MESSAGES,
        source_type="unknown",
        source_id="x",
    )
    UsageReservation.objects.filter(id__in=[held.id, orphan.id]).update(
        created_at=timezone.now() - timezone.timedelta(minutes=5)
    )
    result = settle_finished_reservations()
    assert result["released"] == 1 and result["expired"] == 1
    assert UsageReservation.objects.get(id=held.id).status == UsageReservation.Status.RELEASED
    assert UsageReservation.objects.get(id=orphan.id).status == UsageReservation.Status.EXPIRED
    assert CreditWallet.objects.get(organization=organization).reserved_balance == 0


@pytest.mark.django_db
def test_chat_requests_are_metered_end_to_end(
    django_user_model, django_capture_on_commit_callbacks, settings
):
    from apps.conversations.models import ChatRequest, Conversation

    settings.AI_PROVIDER = "echo"
    organization, owner = make_org(django_user_model, "chatmeter")
    conversation = Conversation.objects.create(owner=owner, organization=organization, title="Metered")
    client = APIClient()
    client.force_authenticate(owner)
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(
            f"/api/v1/conversations/{conversation.id}/messages/",
            {"content": "hello"},
            HTTP_IDEMPOTENCY_KEY="metered-chat",
            HTTP_X_ORGANIZATION_ID=str(organization.id),
        )
    assert response.status_code == 202, response.content
    chat = ChatRequest.objects.get(id=response.json()["id"])
    assert chat.status == ChatRequest.Status.COMPLETED
    record = UsageRecord.objects.get(source_type="chat_request", source_id=str(chat.id))
    assert record.feature == Feature.CHAT_MESSAGES and record.credits_charged > 0
    assert CreditWallet.objects.get(organization=organization).reserved_balance == 0


@pytest.mark.django_db
def test_failed_synchronous_operation_releases_its_hold(django_user_model):
    organization, owner = make_org(django_user_model, "metered-ctx")
    with (
        pytest.raises(RuntimeError),
        metering.metered(
            organization=organization,
            user=owner,
            feature=Feature.IMAGE_GENERATIONS,
            source_type="image_generation",
            source_id="boom",
        ),
    ):
        raise RuntimeError("provider failed")
    assert UsageReservation.objects.get(source_id="boom").status == UsageReservation.Status.RELEASED
    assert not UsageRecord.objects.filter(source_id="boom").exists()
    assert CreditWallet.objects.get(organization=organization).reserved_balance == 0


# --- Rate limiting ------------------------------------------------------------------


def test_rate_limiter_is_atomic_under_concurrency():
    from django.core.cache import caches

    from apps.core.ratelimit import hit

    caches["rate_limits"].clear()
    results = run_concurrently(20, lambda index: hit("race:limiter", limit=7, window=60).allowed)
    assert results.count(True) == 7 and results.count(False) == 13


@pytest.mark.django_db
def test_throttles_enforce_user_and_tenant_limits(django_user_model, settings):
    from django.core.cache import caches

    caches["rate_limits"].clear()
    rates = dict(settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"])
    rates.update({"embeddings": "2/minute", "burst": "100/minute", "ip": "100/minute"})
    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "DEFAULT_THROTTLE_RATES": rates}
    settings.THROTTLE_TENANT_MULTIPLIER = 1
    organization, owner = make_org(django_user_model, "throttle")
    colleague = django_user_model.objects.create_user(username="throttle-2", supabase_user_id="throttle-2")
    UserOrganization.objects.create(user=colleague, organization=organization)
    UserRole.objects.create(
        user=colleague, role=Role.objects.get(name=Role.RoleType.EDITOR), organization=organization
    )

    def post(user):
        client = APIClient()
        client.force_authenticate(user)
        return client.post(
            "/api/v1/embeddings/",
            {"texts": ["x"]},
            format="json",
            HTTP_X_ORGANIZATION_ID=str(organization.id),
        )

    assert [post(owner).status_code for _ in range(2)] == [200, 200]
    limited = post(owner)
    assert limited.status_code == 429 and int(limited["Retry-After"]) >= 1
    # The tenant ceiling (2 x multiplier 1) is already spent, so a colleague is limited too.
    assert post(colleague).status_code == 429


# --- Reconciliation and dashboards --------------------------------------------------


@pytest.mark.django_db
def test_reconciliation_flags_unbilled_runs_and_price_drift(django_user_model):
    from apps.ai_gateway.models import Model, ModelRun
    from apps.usage.tasks import reconcile_day

    organization, owner = make_org(django_user_model, "recon")
    model = Model.objects.select_related("provider").first()
    model.input_price_per_token = Decimal("0.000001")
    model.output_price_per_token = Decimal("0")
    model.save(update_fields=["input_price_per_token", "output_price_per_token"])
    run = ModelRun.objects.create(
        request_id="00000000-0000-0000-0000-000000000001",
        provider=model.provider,
        model=model,
        organization=organization,
        status=ModelRun.Status.COMPLETED,
        input_tokens=1000,
        provider_cost_usd=Decimal("0.5"),  # disagrees with 1000 tokens x 0.000001
    )
    results = reconcile_day(timezone.now().date())
    reconciliation = next(item for item in results if item.organization_id == organization.id)
    assert reconciliation.status == UsageReconciliation.Status.DRIFT
    assert reconciliation.unbilled_runs == 1 and reconciliation.details["priceDrift"] is True
    assert str(run.id) in reconciliation.details["unbilledRunIds"]


@pytest.mark.django_db
def test_tenant_usage_view_and_staff_only_dashboards(django_user_model):
    organization, owner = make_org(django_user_model, "dash")
    for index, feature in enumerate([Feature.CHAT_MESSAGES, Feature.IMAGE_GENERATIONS]):
        held = metering.reserve(
            organization=organization, user=owner, feature=feature, source_type="t", source_id=f"dash-{index}"
        )
        metering.settle(held.id)
    client = APIClient()
    client.force_authenticate(owner)
    usage = client.get("/api/v1/usage/", HTTP_X_ORGANIZATION_ID=str(organization.id)).json()
    assert usage["totalCredits"] == 101.0
    assert usage["byType"] == {"chat": 1.0, "images": 100.0, "documents": 0.0, "agent": 0.0}
    assert set(usage["concurrency"]) == {"jobs", "chat_requests", "agent_runs", "analysis_runs"}
    assert (
        client.get("/api/v1/usage/records/", HTTP_X_ORGANIZATION_ID=str(organization.id)).json()["count"] == 2
    )
    assert client.get("/api/v1/internal/usage/summary/").status_code == 403

    staff = django_user_model.objects.create_user(username="staff", supabase_user_id="staff", is_staff=True)
    client.force_authenticate(staff)
    summary = client.get("/api/v1/internal/usage/summary/").json()
    assert Decimal(summary["totals"]["credits"]) >= Decimal("101")
    organizations = client.get("/api/v1/internal/usage/organizations/").json()
    assert any(row["organizationId"] == str(organization.id) for row in organizations)
    assert client.get("/api/v1/internal/usage/reservations/").status_code == 200
