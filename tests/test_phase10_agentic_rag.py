"""Phase 10 exit proofs on Supabase pgvector + full-text search.

Covers retrieval quality thresholds on the versioned dataset, tenant and
document-ACL isolation at every retrieval/API boundary, citations, evaluation,
ingestion recovery and the frontend knowledge API contract.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.urls import resolve
from django.utils import timezone
from rest_framework.test import APIClient

from apps.identity.models import Organization, Role, UserOrganization, UserRole
from apps.jobs.models import Job
from apps.knowledge import retrieval
from apps.knowledge.embeddings import embed_query
from apps.knowledge.evaluation import load_cases, run_benchmark
from apps.knowledge.models import Chunk, Citation, Collection, Document, DocumentAccessGrant, Source, SyncRun
from apps.knowledge.retrieval import (
    build_context,
    evaluate_response,
    hybrid_retrieve,
    lexical_search,
    persist_citations,
)
from apps.knowledge.tasks import process_document, recover_stalled_ingestion, sync_source
from apps.knowledge.vectorstore import semantic_search

pytestmark = pytest.mark.django_db


def member(django_user_model, organization, label: str, role: str):
    person = django_user_model.objects.create_user(
        username=label, supabase_user_id=f"supabase-{label}", email=f"{label}@example.test"
    )
    UserOrganization.objects.create(user=person, organization=organization)
    role_record, _ = Role.objects.get_or_create(name=role)
    UserRole.objects.get_or_create(user=person, role=role_record, organization=organization)
    return person


@pytest.fixture
def tenant(django_user_model):
    organization = Organization.objects.create(name="RAG Org", slug="rag-org")
    admin = member(django_user_model, organization, "rag-admin", Role.RoleType.ADMIN)
    editor = member(django_user_model, organization, "rag-editor", Role.RoleType.EDITOR)
    viewer = member(django_user_model, organization, "rag-viewer", Role.RoleType.VIEWER)
    return organization, admin, editor, viewer


@pytest.fixture
def other_tenant(django_user_model):
    organization = Organization.objects.create(name="Rival Org", slug="rival-org")
    owner = member(django_user_model, organization, "rival-admin", Role.RoleType.ADMIN)
    return organization, owner


def client_for(person, organization) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=person)
    client.credentials(HTTP_X_ORGANIZATION_ID=str(organization.id))
    return client


def collection_for(organization, user, name="Handbook") -> Collection:
    return Collection.objects.create(
        organization=organization,
        name=name,
        embedding_provider="echo",
        embedding_model="echo-deterministic",
        created_by=user,
    )


def ingest(collection, user, text: str, *, name="Doc", acl=None) -> Document:
    config = {"text": text}
    if acl is not None:
        config["acl"] = acl
    source = Source.objects.create(
        collection=collection, source_type=Source.SourceType.TEXT, name=name, config=config, created_by=user
    )
    sync_source(str(source.id))
    return Document.objects.get(source=source)


# --- Routing -----------------------------------------------------------------


def test_knowledge_routes_do_not_collide_with_document_authoring():
    assert resolve("/api/v1/knowledge/documents/").func.cls.__module__ == "apps.knowledge.views"
    assert resolve("/api/v1/documents/").func.cls.__module__ == "apps.documents.views"
    for path in (
        "/api/v1/knowledge/collections/",
        "/api/v1/knowledge/search/",
        "/api/v1/knowledge/query/",
        "/api/v1/knowledge/sources/00000000-0000-0000-0000-000000000000/sync/",
    ):
        assert resolve(path).func.__module__ == "apps.knowledge.views"


# --- Ingestion ---------------------------------------------------------------


def test_ingestion_stores_versioned_vectors_and_fts_and_completes_sync(tenant):
    organization, admin, _editor, _viewer = tenant
    document = ingest(
        collection_for(organization, admin), admin, "The deployment handbook explains rollback."
    )
    document.refresh_from_db()
    assert document.status == Document.Status.INDEXED
    chunk = document.chunks.get()
    assert chunk.embedding is not None
    assert chunk.embedding_version == "echo/echo-deterministic@1536"
    assert chunk.metadata["source_hash"] == document.content_hash
    run = SyncRun.objects.get(source=document.source)
    assert run.status == SyncRun.Status.COMPLETED and run.chunks_created == 1
    document.source.refresh_from_db()
    assert document.source.status == Source.Status.INDEXED
    assert document.source.processing_started_at is None


def test_reindex_of_unchanged_content_is_idempotent(tenant):
    organization, admin, _editor, _viewer = tenant
    document = ingest(collection_for(organization, admin), admin, "Stable content for idempotency.")
    first = list(document.chunks.values_list("id", flat=True))
    process_document(str(document.id))
    assert list(Document.objects.get(id=document.id).chunks.values_list("id", flat=True)) == first


def test_transient_embedding_failure_requeues_then_fails_after_budget(tenant, settings, monkeypatch):
    from apps.knowledge import embeddings

    settings.RAG_INGESTION_MAX_RETRIES = 2
    organization, admin, _editor, _viewer = tenant

    def outage(texts, **kwargs):
        raise embeddings.TransientEmbeddingError("provider outage")

    monkeypatch.setattr("apps.knowledge.embeddings.embed_texts", outage)
    document = ingest(collection_for(organization, admin), admin, "Content during an outage.")
    document.refresh_from_db()
    assert document.status == Document.Status.FAILED
    assert document.index_attempts == 2
    assert "outage" in document.last_error


def test_recovery_requeues_stalled_documents_and_sources(tenant):
    organization, admin, _editor, _viewer = tenant
    document = ingest(collection_for(organization, admin), admin, "Recoverable content.")
    stale = timezone.now() - timedelta(hours=2)
    Document.objects.filter(id=document.id).update(
        status=Document.Status.EMBEDDING, processing_started_at=stale
    )
    result = recover_stalled_ingestion()
    assert result["requeued"] >= 1
    assert Document.objects.get(id=document.id).status == Document.Status.INDEXED


def test_stuck_processing_source_is_taken_over_after_the_stall_window(tenant):
    organization, admin, _editor, _viewer = tenant
    source = Source.objects.create(
        collection=collection_for(organization, admin),
        source_type=Source.SourceType.TEXT,
        name="Stuck",
        config={"text": "Stuck source text."},
        status=Source.Status.PROCESSING,
        processing_started_at=timezone.now(),
        created_by=admin,
    )
    sync_source(str(source.id))
    assert not SyncRun.objects.filter(source=source).exists()  # fresh claim respected
    Source.objects.filter(id=source.id).update(processing_started_at=timezone.now() - timedelta(hours=2))
    sync_source(str(source.id))
    source.refresh_from_db()
    assert source.status == Source.Status.INDEXED


# --- Retrieval & isolation ---------------------------------------------------


def test_semantic_search_on_pgvector_respects_tenant_acl_and_embedding_version(tenant, other_tenant):
    organization, admin, _editor, viewer = tenant
    rival, rival_admin = other_tenant
    mine = ingest(
        collection_for(organization, admin), admin, "Rotate production credentials every ninety days."
    )
    secret = ingest(
        collection_for(organization, admin, "Restricted"),
        admin,
        "Rotate production credentials every ninety days.",
        acl={"visibility": "restricted", "user_ids": []},
    )
    foreign = ingest(
        collection_for(rival, rival_admin), rival_admin, "Rotate production credentials every ninety days."
    )
    vector = embed_query("Rotate production credentials every ninety days.")
    all_ids = [mine.collection_id, secret.collection_id, foreign.collection_id]

    as_viewer = semantic_search(vector, collection_ids=all_ids, organization_id=organization.id, user=viewer)
    assert {item["document_id"] for item in as_viewer} == {str(mine.id)}
    assert as_viewer[0]["score"] == 1.0
    as_admin = semantic_search(vector, collection_ids=all_ids, organization_id=organization.id, user=admin)
    assert {item["document_id"] for item in as_admin} == {str(mine.id), str(secret.id)}

    Chunk.objects.filter(document=mine).update(embedding_version="openai/text-embedding-3-small@1536")
    stale = semantic_search(vector, collection_ids=all_ids, organization_id=organization.id, user=viewer)
    assert stale == []  # vectors from another embedding space are never compared


def test_lexical_full_text_search_stems_and_enforces_grants(tenant):
    organization, admin, _editor, viewer = tenant
    collection = collection_for(organization, admin)
    restricted = ingest(
        collection,
        admin,
        "Signing keys are rotated by the security team.",
        acl={"visibility": "restricted", "user_ids": []},
    )
    assert (
        lexical_search(
            "rotating signing key",
            collection_ids=[collection.id],
            organization_id=organization.id,
            user=viewer,
            top_k=5,
        )
        == []
    )
    DocumentAccessGrant.objects.create(document=restricted, user=viewer, granted_by=admin)
    results = lexical_search(
        "rotating signing key",
        collection_ids=[collection.id],
        organization_id=organization.id,
        user=viewer,
        top_k=5,
    )
    assert [item["document_id"] for item in results] == [str(restricted.id)]
    assert results[0]["retrieval_methods"] == ["lexical"]


def test_hybrid_model_reranker_and_reported_fallback(tenant, settings, monkeypatch):
    organization, admin, _editor, _viewer = tenant
    collection = collection_for(organization, admin)
    ingest(collection, admin, "Refunds are available within fourteen days.", name="refunds")
    ingest(collection, admin, "Refund shipping labels are printed at the warehouse.", name="shipping")
    settings.RAG_RERANKER = "model"

    def scores(*, system, user_text, **kwargs):
        # Score the passage that mentions "fourteen" highest regardless of fused order.
        passages = user_text.split("<passage id=")[1:]
        return {
            "scores": [
                {"id": int(passage.split(">", 1)[0]), "score": 0.95 if "fourteen" in passage else 0.1}
                for passage in passages
            ]
        }

    monkeypatch.setattr(retrieval, "_gateway_json", scores)
    vector = embed_query("refund window")
    ranked = hybrid_retrieve(
        "refund window",
        vector,
        collection_ids=[collection.id],
        organization_id=organization.id,
        user=admin,
        top_k=2,
    )
    assert ranked.reranker == "model:classification"
    assert "fourteen" in ranked.results[0]["content"]
    assert ranked.degraded == []

    def broken(**kwargs):
        raise ValueError("not json")

    monkeypatch.setattr(retrieval, "_gateway_json", broken)
    fallback = hybrid_retrieve(
        "refund window",
        None,
        collection_ids=[collection.id],
        organization_id=organization.id,
        user=admin,
        top_k=2,
    )
    assert fallback.reranker == "deterministic-hybrid-v1"
    assert fallback.degraded == ["semantic", "reranker"]


def test_versioned_dataset_meets_recall_and_mrr_thresholds(tenant, settings):
    organization, admin, _editor, _viewer = tenant
    report = run_benchmark(load_cases(), organization=organization, user=admin, top_k=5)
    assert len(report.cases) >= 12
    assert report.mean_recall >= settings.RAG_EVAL_MIN_RECALL, report.as_dict()
    assert report.mrr >= settings.RAG_EVAL_MIN_MRR, report.as_dict()
    assert not Collection.objects.filter(name__startswith="rag-eval-").exists()


# --- Context, citations, evaluation ------------------------------------------


def test_context_citations_and_evaluation_are_durable(tenant):
    organization, admin, _editor, _viewer = tenant
    document = ingest(
        collection_for(organization, admin),
        admin,
        "# Runbook\n\nUse the rollback checklist before deployment.",
    )
    chunk = document.chunks.get()
    job = Job.objects.create(
        owner=admin, organization=organization, task_type=Job.TaskType.RAG_QUERY, input_payload={}
    )
    context = build_context(
        [
            {
                "chunk_id": str(chunk.id),
                "document_title": "Runbook",
                "chunk_index": 0,
                "content": chunk.content,
                "heading_path": ["Runbook"],
                "page_number": 3,
            }
        ],
        max_tokens=200,
    )
    assert context.text.startswith("[1] Runbook (chunk 0, page 3, Runbook)")
    sources = persist_citations(job=job, sources=context.sources)
    evaluation = evaluate_response(
        organization=organization,
        job=job,
        query="How do I deploy?",
        sources=sources,
        answer="Use the rollback checklist [1].",
        expected_chunk_ids=[str(chunk.id)],
    )
    assert job.citations.count() == 1
    assert evaluation["passed"] is True and evaluation["mrr"] == 1.0
    assert evaluation["evaluator"] == "heuristic-rag-v1"


def test_model_judge_can_fail_an_answer_with_valid_citation_markers(tenant, settings, monkeypatch):
    organization, admin, _editor, _viewer = tenant
    settings.RAG_JUDGE = "model"
    document = ingest(collection_for(organization, admin), admin, "Use the checklist.")
    chunk = document.chunks.get()
    monkeypatch.setattr(
        retrieval,
        "_gateway_json",
        lambda **kwargs: {
            "supported": False,
            "score": 0.0,
            "abstained": False,
            "unsupported_claims": ["moon"],
        },
    )
    result = evaluate_response(
        organization=organization,
        user=admin,
        query="What should I use?",
        sources=[{"chunk_id": str(chunk.id), "content": chunk.content, "citation_index": 1}],
        answer="Fly to the moon [1].",
    )
    assert result["citationValid"] is True
    assert result["grounded"] is False
    assert result["evaluator"] == "llm-judge:classification"
    assert result["judge"]["unsupportedClaims"] == ["moon"]


def test_agent_knowledge_search_persists_numbered_citations(tenant):
    from apps.agents.engine import _cites_recorded_evidence
    from apps.agents.models import AgentRun
    from apps.agents.tools import _knowledge_search
    from apps.knowledge.evidence import agent_run_evidence

    organization, admin, _editor, _viewer = tenant
    ingest(
        collection_for(organization, admin),
        admin,
        "Expenses above five hundred dollars need director approval.",
    )
    run = AgentRun.objects.create(
        user=admin, organization=organization, input_text="expenses", graph="research"
    )
    with agent_run_evidence(run):
        first = _knowledge_search(admin, organization.id, query="expenses approval")
        second = _knowledge_search(admin, organization.id, query="director approval")
    assert first.splitlines()[1].startswith("[1] ")
    assert second.splitlines()[1].startswith("[2] ")
    assert list(Citation.objects.filter(agent_run=run).values_list("citation_index", flat=True)) == [1, 2]
    run.final_output = "Directors approve large expenses [2]."
    assert _cites_recorded_evidence(run) is True
    run.final_output = "Directors approve large expenses [7]."
    assert _cites_recorded_evidence(run) is False


# --- API: frontend contract & authorization ----------------------------------


def test_frontend_collection_and_source_contract(tenant):
    organization, _admin, editor, _viewer = tenant
    api = client_for(editor, organization)
    created = api.post(
        "/api/v1/knowledge/collections/", {"name": "Docs", "description": "Team docs"}, format="json"
    )
    assert created.status_code == 201, created.content
    collection_id = created.json()["id"]
    assert created.json()["sources"] == []
    assert created.json()["embeddingProvider"] == "echo"

    added = api.post(
        f"/api/v1/knowledge/collections/{collection_id}/sources/",
        {
            "collectionId": collection_id,
            "type": "text",
            "name": "Policy",
            "config": {"text": "Leave is sixteen weeks."},
        },
        format="json",
    )
    assert added.status_code == 201, added.content
    source = added.json()["sources"][0]
    assert source["status"] == "indexed" and source["docCount"] == 1
    assert source["config"] == {"textLength": len("Leave is sixteen weeks.")}

    listed = api.get("/api/v1/knowledge/collections/")
    assert isinstance(listed.json(), list) and listed.json()[0]["id"] == collection_id

    synced = api.post(f"/api/v1/knowledge/sources/{source['id']}/sync/")
    assert synced.status_code == 202 and synced.json()["id"] == source["id"]

    found = api.get(
        "/api/v1/knowledge/search/", {"collectionId": collection_id, "query": "parental leave weeks"}
    )
    assert found.status_code == 200
    assert found.json()[0]["sourceId"] == source["id"] and "sixteen" in found.json()[0]["text"]

    removed = api.delete(f"/api/v1/knowledge/collections/{collection_id}/sources/{source['id']}/")
    assert removed.status_code == 200 and removed.json()["sources"] == []

    unsupported = api.post(
        f"/api/v1/knowledge/collections/{collection_id}/sources/",
        {"type": "integration", "name": "Slack", "config": {}},
        format="json",
    )
    assert unsupported.status_code == 400


def test_sync_query_endpoint_returns_grounded_answer_with_citations(tenant, settings):
    from apps.billing.services import CreditService

    organization, admin, editor, _viewer = tenant
    CreditService.add_credits(CreditService.get_or_create_wallet(organization), 1000, reason="Test credits")
    collection = collection_for(organization, admin)
    ingest(collection, admin, "Every pull request needs two approvals before merging.")
    response = client_for(editor, organization).post(
        "/api/v1/knowledge/query/",
        {"collectionId": str(collection.id), "query": "pull request approvals"},
        format="json",
    )
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["answer"]
    assert body["sources"][0]["citationIndex"] == 1
    job = Job.objects.get(id=body["jobId"])
    assert job.status == Job.Status.COMPLETED
    assert job.citations.count() == len(body["sources"])


def test_patch_cannot_move_collections_or_sources_into_another_tenant(tenant, other_tenant):
    organization, admin, editor, _viewer = tenant
    rival, rival_admin = other_tenant
    collection = collection_for(organization, editor)
    rival_collection = collection_for(rival, rival_admin)
    source = Source.objects.create(
        collection=collection, source_type="text", name="S", config={"text": "x"}, created_by=editor
    )
    api = client_for(editor, organization)
    api.patch(
        f"/api/v1/knowledge/collections/{collection.id}/",
        {"organizationId": str(rival.id), "organization": str(rival.id)},
        format="json",
    )
    api.patch(
        f"/api/v1/knowledge/sources/{source.id}/",
        {"collectionId": str(rival_collection.id), "collection": str(rival_collection.id)},
        format="json",
    )
    collection.refresh_from_db()
    source.refresh_from_db()
    assert collection.organization_id == organization.id
    assert source.collection_id == collection.id


def test_only_source_creator_or_admin_can_change_a_source(tenant, django_user_model):
    organization, admin, editor, _viewer = tenant
    other_editor = member(django_user_model, organization, "rag-editor-2", Role.RoleType.EDITOR)
    source = Source.objects.create(
        collection=collection_for(organization, editor),
        source_type="text",
        name="S",
        config={"text": "x"},
        created_by=editor,
    )
    denied = client_for(other_editor, organization).patch(
        f"/api/v1/knowledge/sources/{source.id}/", {"config": {"text": "rewritten"}}, format="json"
    )
    assert denied.status_code == 403
    allowed = client_for(admin, organization).patch(
        f"/api/v1/knowledge/sources/{source.id}/", {"config": {"text": "rewritten"}}, format="json"
    )
    assert allowed.status_code == 200


def test_restricted_file_document_bytes_are_not_exposed_through_the_asset_api(tenant, settings, monkeypatch):
    from apps.assets.models import Asset

    settings.SUPABASE_URL = "https://project.supabase.co"
    settings.SUPABASE_SECRET_KEY = "sb_secret_test"  # pragma: allowlist secret
    settings.SUPABASE_STORAGE_BUCKET = "jt-code-assets"
    monkeypatch.setattr(
        "apps.assets.views.generate_signed_delivery_url", lambda key: f"https://signed.example.test/{key}"
    )
    organization, admin, editor, viewer = tenant
    asset = Asset.objects.create(
        owner=editor,
        organization=organization,
        storage_object_id="jt-code/restricted.pdf",
        storage_key="jt-code/restricted.pdf",
        storage_bucket="jt-code-assets",
        storage_url="",
        resource_type="non-image",
        format="pdf",
        original_filename="restricted.pdf",
        checksum_sha256="a" * 64,
    )
    assert client_for(viewer, organization).post(f"/api/v1/files/{asset.id}/access/").status_code == 404
    assert client_for(viewer, organization).get("/api/v1/files/").json() == []
    assert client_for(editor, organization).post(f"/api/v1/files/{asset.id}/access/").status_code == 200
    assert client_for(admin, organization).post(f"/api/v1/files/{asset.id}/access/").status_code == 200


def test_document_grants_delete_and_download_endpoints(tenant, settings):
    organization, admin, editor, viewer = tenant
    document = ingest(
        collection_for(organization, editor),
        editor,
        "Restricted salary bands.",
        acl={"visibility": "restricted", "user_ids": []},
    )
    viewer_api = client_for(viewer, organization)
    assert viewer_api.get(f"/api/v1/knowledge/documents/{document.id}/").status_code == 404
    editor_api = client_for(editor, organization)
    granted = editor_api.post(
        f"/api/v1/knowledge/documents/{document.id}/grants/", {"userId": str(viewer.id)}, format="json"
    )
    assert granted.status_code == 201
    assert viewer_api.get(f"/api/v1/knowledge/documents/{document.id}/").status_code == 200
    assert (
        viewer_api.post(
            f"/api/v1/knowledge/documents/{document.id}/grants/", {"userId": str(viewer.id)}, format="json"
        ).status_code
        == 403
    )
    assert (
        editor_api.post(f"/api/v1/knowledge/documents/{document.id}/download/").status_code == 404
    )  # TEXT source

    assert (
        editor_api.delete(f"/api/v1/knowledge/documents/{document.id}/grants/{viewer.id}/").status_code == 204
    )
    assert viewer_api.get(f"/api/v1/knowledge/documents/{document.id}/").status_code == 404

    assert editor_api.delete(f"/api/v1/knowledge/documents/{document.id}/").status_code == 204
    document.refresh_from_db()
    assert document.status == Document.Status.DELETED and not document.chunks.exists()
    assert editor_api.get("/api/v1/knowledge/documents/").json()["count"] == 0


def test_cross_tenant_collections_are_invisible_and_unsearchable(tenant, other_tenant):
    organization, _admin, editor, _viewer = tenant
    rival, rival_admin = other_tenant
    rival_collection = collection_for(rival, rival_admin, "Secret")
    api = client_for(editor, organization)
    assert api.get(f"/api/v1/knowledge/collections/{rival_collection.id}/").status_code == 404
    assert (
        api.get(
            "/api/v1/knowledge/search/", {"collectionId": str(rival_collection.id), "query": "x"}
        ).status_code
        == 404
    )
    assert (
        api.post(
            "/api/v1/knowledge/query/",
            {"collectionId": str(rival_collection.id), "query": "x"},
            format="json",
        ).status_code
        == 404
    )


def test_viewer_cannot_create_collections_or_sources(tenant):
    organization, admin, _editor, viewer = tenant
    collection = collection_for(organization, admin)
    api = client_for(viewer, organization)
    assert api.post("/api/v1/knowledge/collections/", {"name": "x"}, format="json").status_code == 403
    assert (
        api.post(
            f"/api/v1/knowledge/collections/{collection.id}/sources/",
            {"type": "text", "name": "x", "config": {"text": "y"}},
            format="json",
        ).status_code
        == 403
    )


def test_embeddings_endpoint_returns_vectors_in_the_knowledge_embedding_space(tenant):
    organization, _admin, editor, _viewer = tenant
    response = client_for(editor, organization).post(
        "/api/v1/embeddings/", {"texts": ["hello", "world"], "taskType": "query"}, format="json"
    )
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["embeddingVersion"] == "echo/echo-deterministic@1536"
    assert len(body["embeddings"]) == 2 and body["dimensions"] == 1536
