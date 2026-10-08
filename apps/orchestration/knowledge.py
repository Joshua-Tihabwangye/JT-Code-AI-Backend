"""Knowledge integration sources synced by the ``knowledge-integration-sync`` workflow.

A knowledge source of type ``integration`` points at a connected integration
(Google Drive folder, Notion workspace, GitHub repository or Slack channel).
Syncing it emits ``knowledge.integration.sync_requested``; the subscribed n8n
workflow reads the provider with n8n-managed credentials and pushes documents
back through a signed callback. Django owns the result: it upserts one
:class:`~apps.knowledge.models.Document` per external id, indexes changed
documents with the regular pipeline (pgvector + FTS), and - once the final
batch and the completion callback have arrived - removes documents that no
longer exist upstream.
"""

from __future__ import annotations

import hashlib
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.events.outbox import enqueue_outbox_event
from apps.orchestration import client
from apps.orchestration.models import WorkflowEventDelivery
from apps.orchestration.registry import definitions_for_event

SYNC_EVENT = "knowledge.integration.sync_requested"
MAX_DOCUMENTS_PER_CALLBACK = 100


class IntegrationSyncError(ValueError):
    pass


def request_integration_sync(source: Any, sync_run: Any) -> None:
    """Emit the sync request in the caller's transaction (``sync_source``)."""
    from apps.integrations.facade import public_config
    from apps.integrations.models import ConnectorAccount

    account = (
        ConnectorAccount.objects.select_related("connector")
        .filter(id=source.config.get("integrationId"), organization_id=source.collection.organization_id)
        .first()
    )
    if account is None or account.status != ConnectorAccount.Status.ACTIVE:
        raise IntegrationSyncError("The source's integration is not connected.")
    if not client.configured():
        raise IntegrationSyncError("n8n is not configured; integration sources cannot sync.")
    if not definitions_for_event(SYNC_EVENT):
        raise IntegrationSyncError("No active n8n workflow handles knowledge integration sync.")
    enqueue_outbox_event(
        topic=SYNC_EVENT,
        event_key=str(source.id),
        payload={
            "sourceId": str(source.id),
            "collectionId": str(source.collection_id),
            "organization_id": str(source.collection.organization_id),
            "syncRunId": str(sync_run.id),
            "since": source.last_synced_at.isoformat() if source.last_synced_at else None,
            "integration": {
                "id": str(account.id),
                "key": account.connector.slug,
                "config": public_config(account),
            },
        },
    )


def _clean(document: dict[str, Any]) -> dict[str, Any]:
    external_id = str(document.get("externalId") or "").strip()
    text = document.get("text")
    url = str(document.get("url") or "")
    if not external_id or len(external_id) > 500:
        raise IntegrationSyncError("Each document needs an externalId of at most 500 characters.")
    if text is not None and not isinstance(text, str):
        raise IntegrationSyncError(f"{external_id}: text must be a string.")
    if not text and not url:
        raise IntegrationSyncError(f"{external_id}: provide text or an https url.")
    if url and not url.startswith("https://"):
        raise IntegrationSyncError(f"{external_id}: url must use https.")
    if text and len(text.encode()) > settings.RAG_MAX_EXTRACTED_BYTES:
        raise IntegrationSyncError(f"{external_id}: text exceeds RAG_MAX_EXTRACTED_BYTES.")
    return {
        "external_id": external_id,
        "title": str(document.get("title") or external_id)[:500],
        "text": text or "",
        "url": url,
        "mime_type": str(document.get("mimeType") or "text/plain")[:100],
        "external_url": str(document.get("externalUrl") or "")[:1000],
        "updated_at": str(document.get("updatedAt") or "")[:40],
    }


def ingest_documents(source_id: Any, data: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Upsert pushed documents; returns ``(http_status, body)``."""
    from apps.knowledge.access import normalize_acl
    from apps.knowledge.models import Document, Source, SyncRun
    from apps.knowledge.tasks import process_document

    documents = data.get("documents") or []
    if not isinstance(documents, list) or len(documents) > MAX_DOCUMENTS_PER_CALLBACK:
        return 400, {"detail": f"documents must be a list of at most {MAX_DOCUMENTS_PER_CALLBACK} items."}
    try:
        cleaned = [_clean(item) for item in documents if isinstance(item, dict)]
    except IntegrationSyncError as exc:
        return 400, {"detail": str(exc)}
    queued: list[str] = []
    with transaction.atomic():
        source = (
            Source.objects.select_for_update(of=("self",))
            .select_related("collection")
            .filter(id=source_id, source_type=Source.SourceType.INTEGRATION)
            .first()
        )
        if source is None:
            return 404, {"detail": "Integration source not found."}
        delivery = (
            WorkflowEventDelivery.objects.select_for_update(of=("self",))
            .filter(
                id=str(data.get("deliveryId") or ""),
                event_type=SYNC_EVENT,
                payload__sourceId=str(source.id),
                status__in=(WorkflowEventDelivery.Status.DELIVERING, WorkflowEventDelivery.Status.ACCEPTED),
            )
            .first()
        )
        if delivery is None:
            return 409, {"detail": "No open sync delivery for this source.", "code": "no_open_delivery"}
        run_id = delivery.payload.get("syncRunId")
        sync_run = SyncRun.objects.filter(id=str(run_id), source=source).first() if run_id else None
        visibility, _ = normalize_acl(source.config.get("acl"))
        added = updated = 0
        for item in cleaned:
            text_hash = hashlib.sha256((item["text"] or item["url"]).encode()).hexdigest()
            document = Document.objects.filter(source=source, external_id=item["external_id"]).first()
            previous = ((document.metadata or {}).get("integration") or {}) if document else {}
            changed = (
                document is None
                or previous.get("contentHash") != text_hash
                or (document.status == Document.Status.DELETED)
            )
            if document is None:
                document = Document(source=source, collection=source.collection, content_hash="pending")
                added += 1
            elif changed:
                updated += 1
            document.external_id = item["external_id"]
            document.title = item["title"]
            document.mime_type = item["mime_type"]
            document.visibility = visibility
            document.acl = source.config.get("acl") or {}
            document.metadata = {
                "organization_id": str(source.collection.organization_id),
                "integration": {
                    "text": item["text"],
                    "url": item["url"],
                    "externalUrl": item["external_url"],
                    "updatedAt": item["updated_at"],
                    "contentHash": text_hash,
                    "syncRunId": str(sync_run.id) if sync_run else "",
                },
            }
            if changed:
                document.status = Document.Status.PENDING
                document.deleted_at = None
                document.index_attempts = 0
                document.last_error = ""
            document.save()
            if changed:
                queued.append(str(document.id))
        if sync_run is not None:
            sync_run.documents_processed += len(cleaned)
            sync_run.documents_added += added
            sync_run.documents_updated += updated
            sync_run.save(update_fields=["documents_processed", "documents_added", "documents_updated"])
        if data.get("final"):
            delivery.result = {**(delivery.result or {}), "finalBatchReceived": True}
            delivery.save(update_fields=["result", "updated_at"])
        transaction.on_commit(lambda: [process_document.delay(doc_id) for doc_id in queued])
    return 200, {"accepted": len(cleaned), "added": added, "updated": updated, "queued": len(queued)}


def finish_integration_sync(delivery: WorkflowEventDelivery) -> None:
    from apps.integrations.models import ConnectorAccount
    from apps.knowledge.models import Document, Source, SyncRun
    from apps.knowledge.tasks import _fail_sync, soft_delete_document

    payload = delivery.payload or {}
    source_id, run_id = payload.get("sourceId"), payload.get("syncRunId")
    source = Source.objects.filter(id=str(source_id)).first() if source_id else None
    sync_run = SyncRun.objects.filter(id=str(run_id)).first() if run_id else None
    if source is None or sync_run is None or sync_run.status != SyncRun.Status.RUNNING:
        return
    account_ids = [str(value)] if (value := (payload.get("integration") or {}).get("id")) else []
    if delivery.status == WorkflowEventDelivery.Status.FAILED:
        _fail_sync(source, sync_run, delivery.last_error or "Integration sync failed in n8n.")
        ConnectorAccount.objects.filter(id__in=account_ids).update(
            last_error=(delivery.last_error or "Integration sync failed.")[:2000], updated_at=timezone.now()
        )
        return
    deleted = 0
    if (delivery.result or {}).get("finalBatchReceived"):
        stale = (
            Document.objects.filter(source=source)
            .exclude(status=Document.Status.DELETED)
            .exclude(metadata__integration__syncRunId=str(sync_run.id))
        )
        for document in stale:
            soft_delete_document(document)
            deleted += 1
    now = timezone.now()
    sync_run.status = SyncRun.Status.COMPLETED
    sync_run.documents_deleted = deleted
    sync_run.completed_at = now
    sync_run.save(update_fields=["status", "documents_deleted", "completed_at"])
    Source.objects.filter(id=source.id).update(
        status=Source.Status.INDEXED,
        last_synced_at=now,
        processing_started_at=None,
        last_error="",
        document_count=Document.objects.filter(source=source).exclude(status=Document.Status.DELETED).count(),
        updated_at=now,
    )
    ConnectorAccount.objects.filter(id__in=account_ids).update(
        last_sync_at=now, last_error="", updated_at=now
    )
