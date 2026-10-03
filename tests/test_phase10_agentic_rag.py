"""Phase 10 exit proofs: hybrid quality, evidence provenance, and isolation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.identity.models import Organization
from apps.jobs.models import Job
from apps.knowledge.models import Chunk, Collection, Document, DocumentAccessGrant, Source, SyncRun
from apps.knowledge.retrieval import (
    build_context,
    evaluate_response,
    lexical_search,
    persist_citations,
)
from apps.knowledge.scheduling import parse_sync_schedule, source_is_due
from apps.knowledge.tasks import process_document, sync_source


@pytest.fixture
def rag_org(db, user):
    organization = Organization.objects.create(name="RAG Org", slug="rag-org", owner=user)
    user.organizations.add(organization)
    return organization


def _collection(org, user, name="Knowledge"):
    return Collection.objects.create(
        organization=org,
        name=name,
        embedding_provider="echo",
        embedding_model="echo-deterministic",
        embedding_dimensions=1536,
        created_by=user,
    )


@pytest.mark.django_db
def test_source_sync_ingests_text_and_records_embedding_provenance(settings, rag_org, user):
    settings.PGVECTOR_ENABLED = False
    source = Source.objects.create(
        collection=_collection(rag_org, user),
        source_type=Source.SourceType.TEXT,
        name="Handbook",
        config={"text": "The deployment handbook explains rollback procedures."},
        created_by=user,
    )

    sync_source(str(source.id))
    document = source.documents.get()
    process_document(str(document.id))

    document.refresh_from_db()
    source.refresh_from_db()
    assert document.status == Document.Status.INDEXED
    assert source.status == Source.Status.INDEXED
    chunk = document.chunks.get()
    assert chunk.metadata["source_hash"] == document.content_hash


@pytest.mark.django_db
def test_lexical_retrieval_rechecks_tenant_before_returning_chunks(rag_org, user):
    collection = _collection(rag_org, user)
    source = Source.objects.create(collection=collection, source_type="text", name="Public")
    document = Document.objects.create(
        source=source,
        collection=collection,
        title="Rollback",
        content_hash="hash",
        status=Document.Status.INDEXED,
    )
    chunk = Chunk.objects.create(
        document=document, collection=collection, chunk_index=0, content="Rollback deployment safely"
    )
    other = Organization.objects.create(name="Other RAG", slug="other-rag")
    other_collection = _collection(other, user, "Other")
    other_source = Source.objects.create(collection=other_collection, source_type="text", name="Secret")
    other_document = Document.objects.create(
        source=other_source,
        collection=other_collection,
        title="Secret",
        content_hash="hash",
        status=Document.Status.INDEXED,
    )
    Chunk.objects.create(
        document=other_document,
        collection=other_collection,
        chunk_index=0,
        content="Rollback secret database",
    )

    results = lexical_search(
        "rollback",
        collection_ids=[collection.id, other_collection.id],
        organization_id=rag_org.id,
        user=user,
        top_k=10,
    )

    assert [result["chunk_id"] for result in results] == [str(chunk.id)]


@pytest.mark.django_db
def test_context_citations_and_evaluation_are_durable(rag_org, user):
    collection = _collection(rag_org, user)
    source = Source.objects.create(collection=collection, source_type="text", name="Runbook")
    document = Document.objects.create(
        source=source,
        collection=collection,
        title="Runbook",
        content_hash="hash",
        status=Document.Status.INDEXED,
    )
    chunk = Chunk.objects.create(
        document=document,
        collection=collection,
        chunk_index=0,
        content="Use the rollback checklist before deployment.",
    )
    job = Job.objects.create(
        owner=user, organization=rag_org, task_type=Job.TaskType.RAG_QUERY, input_payload={}
    )
    context = build_context(
        [
            {
                "chunk_id": str(chunk.id),
                "document_id": str(document.id),
                "document_title": "Runbook",
                "chunk_index": 0,
                "content": chunk.content,
            }
        ],
        max_tokens=100,
    )
    sources = persist_citations(job=job, sources=context.sources)
    evaluation = evaluate_response(
        organization=rag_org,
        job=job,
        query="How do I deploy?",
        sources=sources,
        answer="Use the rollback checklist [1].",
        expected_chunk_ids=[str(chunk.id)],
    )

    assert context.text.startswith("[1] Runbook")
    assert job.citations.count() == 1
    assert evaluation["passed"] is True
    assert job.rag_evaluation.metrics["citationValid"] is True


@pytest.mark.django_db
def test_restricted_document_requires_creator_admin_or_explicit_grant(rag_org, user, django_user_model):
    viewer = django_user_model.objects.create_user(
        username="restricted-viewer",
        supabase_user_id="restricted-viewer",
        email="viewer@example.test",
    )
    viewer.organizations.add(rag_org)
    collection = _collection(rag_org, user)
    source = Source.objects.create(collection=collection, source_type="text", name="Restricted")
    document = Document.objects.create(
        source=source,
        collection=collection,
        title="Restricted runbook",
        content_hash="hash",
        status=Document.Status.INDEXED,
        visibility=Document.Visibility.RESTRICTED,
    )
    chunk = Chunk.objects.create(
        document=document,
        collection=collection,
        chunk_index=0,
        content="Rotate the restricted signing key.",
    )

    denied = lexical_search(
        "restricted signing",
        collection_ids=[collection.id],
        organization_id=rag_org.id,
        user=viewer,
        top_k=10,
    )
    assert denied == []

    DocumentAccessGrant.objects.create(document=document, user=viewer, granted_by=user)
    allowed = lexical_search(
        "restricted signing",
        collection_ids=[collection.id],
        organization_id=rag_org.id,
        user=viewer,
        top_k=10,
    )
    assert [item["chunk_id"] for item in allowed] == [str(chunk.id)]


@pytest.mark.django_db
def test_evaluation_fails_when_evidence_answer_has_no_citation(rag_org, user):
    collection = _collection(rag_org, user)
    source = Source.objects.create(collection=collection, source_type="text", name="Runbook")
    document = Document.objects.create(
        source=source,
        collection=collection,
        title="Runbook",
        content_hash="hash",
        status=Document.Status.INDEXED,
    )
    chunk = Chunk.objects.create(
        document=document, collection=collection, chunk_index=0, content="Use the checklist."
    )

    result = evaluate_response(
        organization=rag_org,
        user=user,
        query="What should I use?",
        sources=[{"chunk_id": str(chunk.id), "content": chunk.content}],
        answer="Use the checklist.",
        expected_chunk_ids=[str(chunk.id)],
    )

    assert result["citationValid"] is False
    assert result["grounded"] is False
    assert result["passed"] is False


@pytest.mark.django_db
def test_sync_materializes_restricted_grants_and_completes_run(settings, rag_org, user, django_user_model):
    settings.PGVECTOR_ENABLED = False
    viewer = django_user_model.objects.create_user(
        username="sync-viewer",
        supabase_user_id="sync-viewer",
        email="sync-viewer@example.test",
    )
    viewer.organizations.add(rag_org)
    source = Source.objects.create(
        collection=_collection(rag_org, user),
        source_type=Source.SourceType.TEXT,
        name="Restricted handbook",
        config={
            "text": "Restricted production handbook.",
            "acl": {"visibility": "restricted", "user_ids": [str(viewer.id)]},
        },
        created_by=user,
    )

    sync_source(str(source.id))

    document = source.documents.get()
    run = SyncRun.objects.get(source=source)
    assert document.visibility == Document.Visibility.RESTRICTED
    assert document.access_grants.filter(user=viewer).exists()
    assert run.status == SyncRun.Status.COMPLETED
    assert run.documents_added == 1
    assert run.chunks_created > 0


@pytest.mark.django_db
def test_source_scheduling_is_validated_and_manual_sources_do_not_repeat(rag_org, user):
    source = Source.objects.create(
        collection=_collection(rag_org, user),
        source_type=Source.SourceType.TEXT,
        name="Manual",
        status=Source.Status.INDEXED,
        last_synced_at=source_time(),
        sync_schedule="",
    )
    assert source_is_due(source) is False
    source.status = Source.Status.PENDING
    assert source_is_due(source) is True
    assert parse_sync_schedule("@hourly") is not None
    with pytest.raises(ValueError):
        parse_sync_schedule("not a cron")


def source_time():
    from django.utils import timezone

    return timezone.now()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "case",
    json.loads(
        (Path(__file__).parent / "fixtures" / "rag_regression_cases.json").read_text(encoding="utf-8")
    ),
    ids=lambda case: case["name"],
)
def test_versioned_rag_regression_dataset_meets_recall_threshold(case, settings, rag_org, user):
    collection = _collection(rag_org, user)
    label_to_id = {}
    for index, item in enumerate(case["documents"]):
        source = Source.objects.create(collection=collection, source_type="text", name=f"Regression {index}")
        document = Document.objects.create(
            source=source,
            collection=collection,
            title=item["label"],
            content_hash=f"hash-{index}",
            status=Document.Status.INDEXED,
        )
        chunk = Chunk.objects.create(
            document=document,
            collection=collection,
            chunk_index=0,
            content=item["content"],
        )
        label_to_id[item["label"]] = str(chunk.id)

    results = lexical_search(
        case["query"],
        collection_ids=[collection.id],
        organization_id=rag_org.id,
        user=user,
        top_k=5,
    )
    retrieved = {item["chunk_id"] for item in results}
    expected = {label_to_id[label] for label in case["expected_labels"]}
    recall = len(retrieved & expected) / len(expected)
    assert recall >= settings.RAG_EVAL_MIN_RECALL
