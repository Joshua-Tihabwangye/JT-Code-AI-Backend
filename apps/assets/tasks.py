"""Lifecycle jobs for private Supabase Storage objects.

* ``purge_deleted_assets`` removes provider files after the recovery window.
* ``reconcile_assets`` verifies READY records in bounded batches (each asset at
  most every ``ASSET_RECONCILE_INTERVAL_HOURS``) and quarantines records whose
  provider object disappeared or changed version.
* ``sweep_orphans`` walks the application's Supabase Storage prefix and deletes
  aged files that no record references (e.g. a crash between upload and
  registration).
"""

from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task
def purge_deleted_assets() -> int:
    """Purge soft-deleted provider files after the recovery window, idempotently."""
    from apps.assets.models import Asset
    from apps.assets.supabase_storage import delete_storage_object, supabase_storage_is_configured

    if not supabase_storage_is_configured():
        return 0
    cutoff = timezone.now() - timedelta(days=settings.ASSET_DELETE_GRACE_DAYS)
    count = 0
    asset_ids = Asset.objects.filter(
        status=Asset.Status.DELETED,
        deleted_at__lte=cutoff,
        provider_deleted_at__isnull=True,
        deletion_attempts__lt=settings.ASSET_DELETE_MAX_ATTEMPTS,
    ).values_list("id", flat=True)
    for asset_id in list(asset_ids):
        with transaction.atomic():
            asset = Asset.objects.select_for_update(skip_locked=True).filter(id=asset_id).first()
            if asset is None or asset.provider_deleted_at is not None or asset.status != Asset.Status.DELETED:
                continue
            asset.deletion_attempts += 1
            try:
                delete_storage_object(asset.storage_key)
            except Exception as exc:  # retried by the next sweep until the attempt cap
                asset.deletion_error = str(exc)[:1000]
                asset.save(update_fields=["deletion_attempts", "deletion_error", "updated_at"])
                if asset.deletion_attempts >= settings.ASSET_DELETE_MAX_ATTEMPTS:
                    logger.error(
                        "Asset provider deletion exhausted retries", extra={"asset_id": str(asset.id)}
                    )
                continue
            asset.deletion_error = ""
            asset.provider_deleted_at = timezone.now()
            asset.save(
                update_fields=["deletion_attempts", "deletion_error", "provider_deleted_at", "updated_at"]
            )
            count += 1
    return count


@shared_task
def reconcile_asset(asset_id: str) -> bool:
    """Verify one READY record without quarantining on transient outages."""
    from apps.assets.models import Asset
    from apps.assets.supabase_storage import (
        FINGERPRINT_VERSION,
        SupabaseStorageNotFound,
        content_checksum,
        provider_identity_fingerprint,
    )

    asset = Asset.objects.filter(id=asset_id, status=Asset.Status.READY).first()
    if asset is None:
        return False
    try:
        checksum, _content_type, _head = content_checksum(asset.storage_key, expected_size=asset.bytes)
    except SupabaseStorageNotFound:
        asset.status = Asset.Status.QUARANTINED
        asset.deletion_error = "Provider object no longer exists."
        asset.save(update_fields=["status", "deletion_error", "updated_at"])
        return False
    except Exception as exc:
        asset.deletion_error = f"Transient provider verification error: {exc}"[:1000]
        asset.save(update_fields=["deletion_error", "updated_at"])
        return False
    fingerprint = provider_identity_fingerprint(
        {"bucket": asset.storage_bucket, "key": asset.storage_key, "size": asset.bytes}
    )
    identity_matches = bool(asset.storage_key and asset.storage_bucket)
    if identity_matches and (asset.provenance or {}).get("fingerprintVersion") != FINGERPRINT_VERSION:
        # Records registered before the version-based fingerprint are upgraded
        # once their path and size are confirmed.
        asset.provider_fingerprint = fingerprint
        asset.provenance = {**(asset.provenance or {}), "fingerprintVersion": FINGERPRINT_VERSION}
    if not identity_matches or checksum != asset.checksum_sha256 or fingerprint != asset.provider_fingerprint:
        asset.status = Asset.Status.QUARANTINED
        asset.deletion_error = "Storage object identity, size or checksum changed after registration."
        asset.save(update_fields=["status", "deletion_error", "updated_at"])
        return False
    asset.deletion_error = ""
    asset.last_verified_at = timezone.now()
    asset.save(
        update_fields=[
            "provider_fingerprint",
            "provenance",
            "deletion_error",
            "last_verified_at",
            "updated_at",
        ]
    )
    return True


@shared_task
def reconcile_assets() -> dict[str, int]:
    """Verify the next batch of READY records that are due for re-verification."""
    from apps.assets.models import Asset
    from apps.assets.supabase_storage import supabase_storage_is_configured

    if not supabase_storage_is_configured():
        return {"verified": 0, "quarantined": 0}
    due = timezone.now() - timedelta(hours=settings.ASSET_RECONCILE_INTERVAL_HOURS)
    batch = list(
        Asset.objects.filter(status=Asset.Status.READY)
        .filter(Q(last_verified_at__isnull=True) | Q(last_verified_at__lt=due))
        .order_by("last_verified_at", "created_at")
        .values_list("id", flat=True)[: settings.ASSET_RECONCILE_BATCH_SIZE]
    )
    verified = sum(1 for asset_id in batch if reconcile_asset(str(asset_id)))
    quarantined = Asset.objects.filter(id__in=batch, status=Asset.Status.QUARANTINED).count()
    return {"verified": verified, "quarantined": quarantined}


@shared_task
def sweep_orphans() -> dict[str, int]:
    """Delete aged provider files under the app root that no record references."""
    from apps.assets.models import Asset, UploadIntent
    from apps.assets.supabase_storage import (
        delete_storage_object,
        provider_created_at,
        storage_prefix,
        supabase_storage_is_configured,
        walk_storage_objects,
    )

    if not supabase_storage_is_configured():
        return {"scanned": 0, "orphans_deleted": 0}
    cutoff = timezone.now() - timedelta(hours=settings.ASSET_ORPHAN_GRACE_HOURS)
    scanned = deleted = 0
    for resource in walk_storage_objects(
        storage_prefix(),
        max_depth=settings.ASSET_RECONCILE_MAX_DEPTH,
        page_size=settings.ASSET_RECONCILE_PAGE_SIZE,
        max_pages=settings.ASSET_RECONCILE_MAX_PAGES,
    ):
        scanned += 1
        storage_key = str(resource.get("key") or "")
        created_at = provider_created_at(resource)
        if not storage_key or created_at is None or created_at > cutoff:
            continue
        if Asset.objects.filter(storage_key=storage_key).exists():
            continue
        if UploadIntent.objects.filter(storage_object_key=storage_key).exists():
            continue
        delete_storage_object(storage_key)
        deleted += 1
    return {"scanned": scanned, "orphans_deleted": deleted}


@shared_task
def expire_upload_intents() -> int:
    from apps.assets.models import UploadIntent

    return UploadIntent.objects.filter(
        status=UploadIntent.Status.PENDING, expires_at__lte=timezone.now()
    ).update(status=UploadIntent.Status.EXPIRED)
