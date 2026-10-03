"""Lifecycle jobs for provider-owned assets."""

from __future__ import annotations

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone


@shared_task
def purge_deleted_assets() -> int:
    """Purge soft-deleted remote files after the recovery window, idempotently."""
    from apps.assets.imagekit import delete_imagekit_file, imagekit_is_configured
    from apps.assets.models import Asset

    if not imagekit_is_configured():
        return 0
    cutoff = timezone.now() - timezone.timedelta(days=settings.ASSET_DELETE_GRACE_DAYS)
    count = 0
    asset_ids = Asset.objects.filter(
        status=Asset.Status.DELETED, deleted_at__lte=cutoff, provider_deleted_at__isnull=True
    ).values_list("id", flat=True)
    for asset_id in asset_ids.iterator():
        with transaction.atomic():
            asset = Asset.objects.select_for_update().get(id=asset_id)
            if asset.provider_deleted_at is not None:
                continue
        try:
            delete_imagekit_file(asset.imagekit_file_id)
        except Exception as exc:  # provider failure is retried by the next sweep
            asset.deletion_attempts += 1
            asset.deletion_error = str(exc)[:1000]
            asset.save(update_fields=["deletion_attempts", "deletion_error", "updated_at"])
            continue
        asset.deletion_attempts += 1
        asset.deletion_error = ""
        asset.provider_deleted_at = timezone.now()
        asset.save(update_fields=["deletion_attempts", "deletion_error", "provider_deleted_at", "updated_at"])
        count += 1
    return count


@shared_task
def reconcile_asset(asset_id: str) -> bool:
    """Verify a local ready record without quarantining transient outages."""
    from apps.assets.imagekit import (
        ImageKitNotFound,
        provider_identity_fingerprint,
        verify_imagekit_file,
    )
    from apps.assets.models import Asset

    asset = Asset.objects.filter(id=asset_id, status=Asset.Status.READY).first()
    if asset is None:
        return False
    try:
        resource = verify_imagekit_file(asset.imagekit_file_id)
    except ImageKitNotFound:
        asset.status = Asset.Status.QUARANTINED
        asset.deletion_error = "Provider object no longer exists."
        asset.save(update_fields=["status", "deletion_error", "updated_at"])
        return False
    except Exception as exc:
        asset.deletion_error = f"Transient provider verification error: {exc}"[:1000]
        asset.save(update_fields=["deletion_error", "updated_at"])
        return False
    mismatched = (
        resource.get("filePath") != asset.imagekit_file_path
        or int(resource.get("size", -1)) != asset.bytes
        or provider_identity_fingerprint(resource) != asset.provider_fingerprint
    )
    if mismatched:
        asset.status = Asset.Status.QUARANTINED
        asset.deletion_error = "Provider identity or metadata changed after registration."
        asset.save(update_fields=["status", "deletion_error", "updated_at"])
        return False
    if asset.deletion_error:
        asset.deletion_error = ""
        asset.save(update_fields=["deletion_error", "updated_at"])
    return True


@shared_task
def reconcile_assets() -> dict[str, int]:
    """Reconcile local records and delete aged provider orphans under the app root."""
    from apps.assets.imagekit import (
        delete_imagekit_file,
        imagekit_is_configured,
        list_imagekit_files,
        provider_created_at,
    )
    from apps.assets.models import Asset

    if not imagekit_is_configured():
        return {"verified": 0, "quarantined": 0, "orphans_deleted": 0}
    verified = quarantined = orphans_deleted = 0
    for asset_id in Asset.objects.filter(status=Asset.Status.READY).values_list("id", flat=True).iterator():
        if reconcile_asset(str(asset_id)):
            verified += 1
        elif Asset.objects.filter(id=asset_id, status=Asset.Status.QUARANTINED).exists():
            quarantined += 1

    known_ids = set(Asset.objects.values_list("imagekit_file_id", flat=True))
    cutoff = timezone.now() - timezone.timedelta(hours=settings.ASSET_ORPHAN_GRACE_HOURS)
    root = "/" + settings.IMAGEKIT_UPLOAD_FOLDER.strip("/")
    page_size = settings.IMAGEKIT_RECONCILE_PAGE_SIZE
    orphan_ids: list[str] = []
    for page in range(settings.IMAGEKIT_RECONCILE_MAX_PAGES):
        resources = list_imagekit_files(path=root, skip=page * page_size, limit=page_size)
        for resource in resources:
            file_id = str(resource.get("fileId") or "")
            created_at = provider_created_at(resource)
            if not file_id or file_id in known_ids or created_at is None or created_at > cutoff:
                continue
            orphan_ids.append(file_id)
        if len(resources) < page_size:
            break
    for file_id in orphan_ids:
        delete_imagekit_file(file_id)
        orphans_deleted += 1
    return {
        "verified": verified,
        "quarantined": quarantined,
        "orphans_deleted": orphans_deleted,
    }


@shared_task
def expire_upload_intents() -> int:
    from apps.assets.models import UploadIntent

    return UploadIntent.objects.filter(
        status=UploadIntent.Status.PENDING, expires_at__lte=timezone.now()
    ).update(status=UploadIntent.Status.EXPIRED)
