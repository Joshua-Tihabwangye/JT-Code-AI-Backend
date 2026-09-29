"""Phase 5 proofs for durable job dispatch, cancellation, and queue policy."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.conf import settings
from django.utils import timezone

from apps.events.models import OutboxEvent
from apps.identity.models import Organization
from apps.jobs.dispatch import (
    ANALYSIS_QUEUE,
    INGESTION_QUEUE,
    VISUALIZATION_QUEUE,
    enqueue_job,
    queue_for_task_type,
)
from apps.jobs.executor import _mark_started, execute_job
from apps.jobs.metrics import queue_depths
from apps.jobs.models import Job, JobStep, WorkflowRun
from apps.jobs.tasks import execute_job_task, recover_stalled_jobs, retry_delay_seconds


@pytest.fixture
def org(user):
    organization = Organization.objects.create(name="Phase 5 Org", owner=user)
    user.organizations.add(organization)
    return organization


def make_job(user, org, task_type=Job.TaskType.GENERAL_QUESTION, **kwargs):
    return Job.objects.create(
        owner=user,
        organization=org,
        task_type=task_type,
        trace_id="phase5-test",
        input_payload={"prompt": "test"},
        **kwargs,
    )


def test_workload_policy_assigns_isolated_queues():
    assert queue_for_task_type(Job.TaskType.GENERAL_QUESTION) == ANALYSIS_QUEUE
    assert queue_for_task_type(Job.TaskType.KNOWLEDGE_INGESTION) == INGESTION_QUEUE
    assert queue_for_task_type(Job.TaskType.IMAGE_GENERATION) == VISUALIZATION_QUEUE
    assert queue_for_task_type(Job.TaskType.SCHEDULED_AUTOMATION) == "jobs.default"


@pytest.mark.django_db
def test_enqueue_job_persists_queue_workflow_event_and_task_id(
    user, org, monkeypatch, django_capture_on_commit_callbacks
):
    job = make_job(user, org)
    calls = []

    def fake_apply_async(*, args, queue):
        calls.append({"args": args, "queue": queue})
        return SimpleNamespace(id="celery-phase5-task")

    monkeypatch.setattr("apps.jobs.tasks.execute_job_task.apply_async", fake_apply_async)
    with django_capture_on_commit_callbacks(execute=True):
        enqueue_job(job)

    job.refresh_from_db()
    assert job.queue_name == ANALYSIS_QUEUE
    assert job.celery_task_id == "celery-phase5-task"
    assert calls == [{"args": [str(job.id)], "queue": ANALYSIS_QUEUE}]
    assert WorkflowRun.objects.filter(job=job).exists()
    assert OutboxEvent.objects.filter(topic="jobs.job.created", event_key=str(job.request_id)).exists()


@pytest.mark.django_db
def test_cancelled_job_is_never_executed(user, org):
    job = make_job(user, org, status=Job.Status.CANCELLED)

    result = execute_job(job)

    assert result == {"status": "cancelled", "task_type": job.task_type, "idempotent": True}
    assert not JobStep.objects.filter(job=job).exists()


@pytest.mark.django_db
def test_running_job_is_idempotently_claimed_once(user, org):
    job = make_job(user, org, status=Job.Status.RUNNING)

    result = execute_job(job)

    assert result == {"status": "running", "task_type": job.task_type, "idempotent": True}
    assert not JobStep.objects.filter(job=job).exists()


@pytest.mark.django_db
@pytest.mark.django_db
def test_recovery_requeues_only_stale_native_jobs(user, org, monkeypatch, settings):
    settings.JOB_STALLED_TIMEOUT_SECONDS = 60
    job = make_job(
        user,
        org,
        status=Job.Status.RUNNING,
        queue_name=ANALYSIS_QUEUE,
        started_at=timezone.now() - timedelta(seconds=61),
    )
    external_job = make_job(
        user,
        org,
        task_type=Job.TaskType.IMAGE_GENERATION,
        status=Job.Status.RUNNING,
        queue_name=VISUALIZATION_QUEUE,
        started_at=timezone.now() - timedelta(seconds=61),
    )
    calls = []

    def fake_apply_async(*, args, queue):
        calls.append({"args": args, "queue": queue})
        return SimpleNamespace(id="recovered-task")

    monkeypatch.setattr("apps.jobs.tasks.execute_job_task.apply_async", fake_apply_async)

    assert recover_stalled_jobs() == 1

    job.refresh_from_db()
    external_job.refresh_from_db()
    assert job.status == Job.Status.QUEUED
    assert job.celery_task_id == "recovered-task"
    assert job.error_code == "WORKER_RECOVERY"
    assert external_job.status == Job.Status.RUNNING
    assert calls == [{"args": [str(job.id)], "queue": ANALYSIS_QUEUE}]


@pytest.mark.django_db
def test_recovered_job_reuses_its_unfinished_step(user, org):
    job = make_job(user, org)
    active_step = JobStep.objects.create(
        job=job,
        name=job.task_type,
        step_order=0,
        status=JobStep.Status.RUNNING,
    )

    claimed_step = _mark_started(job)

    assert claimed_step.id == active_step.id


@pytest.mark.django_db
def test_transient_failure_is_persisted_after_retry_limit(user, org, monkeypatch):
    job = make_job(user, org, max_retries=0)
    step = JobStep.objects.create(
        job=job,
        name=job.task_type,
        step_order=0,
        status=JobStep.Status.RUNNING,
    )

    def transient_failure(_job):
        raise TimeoutError("provider timed out")

    monkeypatch.setattr("apps.jobs.executor.execute_job", transient_failure)

    result = execute_job_task.delay(str(job.id)).get()

    job.refresh_from_db()
    assert result == {
        "status": "failed",
        "task_type": job.task_type,
        "error_code": "WORKER_RETRY",
    }
    assert job.status == Job.Status.FAILED
    assert job.retry_count == 1
    assert job.error_code == "WORKER_RETRY"
    step.refresh_from_db()
    assert step.status == JobStep.Status.FAILED


def test_queue_depths_are_derived_from_durable_statuses(user, org):
    make_job(user, org, status=Job.Status.QUEUED, queue_name=ANALYSIS_QUEUE)
    make_job(user, org, status=Job.Status.RUNNING, queue_name=ANALYSIS_QUEUE)
    make_job(user, org, status=Job.Status.WAITING_APPROVAL, queue_name=INGESTION_QUEUE)
    make_job(user, org, status=Job.Status.COMPLETED, queue_name=VISUALIZATION_QUEUE)

    depths = {row["queue"]: row for row in queue_depths()}

    assert depths[ANALYSIS_QUEUE] == {
        "queue": ANALYSIS_QUEUE,
        "queued": 1,
        "running": 1,
        "waitingApproval": 0,
    }
    assert depths[INGESTION_QUEUE]["waitingApproval"] == 1
    assert VISUALIZATION_QUEUE not in depths


def test_retry_backoff_is_bounded_and_jittered(monkeypatch):
    monkeypatch.setattr("apps.jobs.tasks.random.randint", lambda _start, _end: 3)

    assert retry_delay_seconds(0) == 4
    assert retry_delay_seconds(5) == 35
    assert retry_delay_seconds(20) == 303


def test_redis_namespaces_and_worker_safety_settings_are_configured():
    assert {"default", "rate_limits", "job_locks"} <= set(settings.CACHES)
    assert settings.CACHES["rate_limits"]["KEY_PREFIX"].endswith("rate-limit")
    assert settings.CELERY_TASK_ACKS_LATE is True
    assert settings.CELERY_TASK_REJECT_ON_WORKER_LOST is True
    assert settings.CELERY_WORKER_PREFETCH_MULTIPLIER == 1
