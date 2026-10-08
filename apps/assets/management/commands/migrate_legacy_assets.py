"""Finish the one-time cutover of asset records to Supabase Storage.

Before this command is run, copy any retained provider bytes into the private
Supabase bucket using your approved transfer path. Then map each quarantined
row to its new bucket key, for example::

    python manage.py migrate_legacy_assets --map <asset-uuid>=jt-code/<org>/uploads/<file>

The command downloads and hashes the mapped object before making it available.
``--local-fallbacks`` also registers development-only local generated files.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.assets.models import Asset
from apps.assets.services import register_generated_asset
from apps.assets.supabase_storage import (
    FINGERPRINT_VERSION,
    SupabaseStorageError,
    content_checksum,
    provider_identity_fingerprint,
    supabase_storage_is_configured,
)

_ROOT = Path(settings.BASE_DIR)
_DOCUMENT_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


class Command(BaseCommand):
    help = "Map imported legacy objects to Supabase Storage and register local fallback files."

    def add_arguments(self, parser):
        parser.add_argument("--map", action="append", default=[], metavar="ASSET_ID=STORAGE_KEY")
        parser.add_argument("--local-fallbacks", action="store_true")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: Any, **options: Any) -> None:
        if not supabase_storage_is_configured() and not options["dry_run"]:
            raise CommandError("Supabase Storage must be configured to migrate assets.")
        mapped = sum(self._map(pair, dry_run=options["dry_run"]) for pair in options["map"])
        uploaded = self._local_fallbacks(dry_run=options["dry_run"]) if options["local_fallbacks"] else 0
        self.stdout.write(self.style.SUCCESS(f"mapped={mapped} uploaded={uploaded}"))

    def _map(self, pair: str, *, dry_run: bool) -> int:
        asset_id, separator, storage_key = pair.partition("=")
        asset = Asset.objects.filter(id=asset_id).first()
        if asset is None or not separator or not storage_key:
            raise CommandError(f"Invalid mapping {pair!r}: expected <asset-uuid>=<storage-key>.")
        if dry_run:
            self.stdout.write(f"would map {asset.id} -> {storage_key}")
            return 0
        try:
            checksum, content_type, _head = content_checksum(storage_key, expected_size=asset.bytes)
        except SupabaseStorageError as exc:
            raise CommandError(f"Could not verify {storage_key!r}: {exc}") from exc
        with transaction.atomic():
            asset.storage_object_id = storage_key
            asset.storage_key = storage_key
            asset.storage_bucket = settings.SUPABASE_STORAGE_BUCKET
            asset.storage_url = ""
            asset.checksum_sha256 = checksum
            asset.provider_fingerprint = provider_identity_fingerprint(
                {"bucket": asset.storage_bucket, "key": storage_key, "size": asset.bytes}
            )
            asset.status = Asset.Status.READY
            asset.deletion_error = ""
            asset.last_verified_at = timezone.now()
            asset.metadata = {**(asset.metadata or {}), "content_type": content_type}
            asset.provenance = {
                **(asset.provenance or {}),
                "provider": "supabase-storage",
                "migration_required": False,
                "migrated_at": timezone.now().isoformat(),
                "fingerprintVersion": FINGERPRINT_VERSION,
            }
            asset.save()
        self.stdout.write(f"mapped {asset.id} -> {storage_key}")
        return 1

    def _local_fallbacks(self, *, dry_run: bool) -> int:
        from apps.ai_gateway.models import GeneratedImage
        from apps.conversions.models import ConversionJob
        from apps.documents.models import Document

        uploaded = 0
        images = GeneratedImage.objects.filter(asset__isnull=True).select_related("organization", "owner")
        for image in images:
            path = _ROOT / "generated_images" / f"{image.id}.png"
            if path.exists():
                uploaded += self._upload(
                    path,
                    image,
                    "image/png",
                    kind="images",
                    link="asset",
                    clear={"storage_url": ""},
                    dry_run=dry_run,
                )
        for document in Document.objects.filter(rendered_asset__isnull=True).exclude(download_url=""):
            for fmt, content_type in _DOCUMENT_TYPES.items():
                path = _ROOT / "rendered_documents" / f"{document.id}.{fmt}"
                if path.exists():
                    uploaded += self._upload(
                        path,
                        document,
                        content_type,
                        kind="documents",
                        link="rendered_asset",
                        clear={"download_url": ""},
                        dry_run=dry_run,
                    )
                    break
        for job in ConversionJob.objects.filter(output_asset__isnull=True).exclude(output_path=""):
            path = Path(job.output_path)
            if path.exists() and _ROOT in path.resolve().parents:
                uploaded += self._upload(
                    path,
                    job,
                    "application/octet-stream",
                    kind="conversions",
                    link="output_asset",
                    clear={"output_url": ""},
                    dry_run=dry_run,
                )
        return uploaded

    def _upload(
        self,
        path: Path,
        row: Any,
        content_type: str,
        *,
        kind: str,
        link: str,
        clear: dict[str, str],
        dry_run: bool,
    ) -> int:
        if dry_run:
            self.stdout.write(f"would upload {path} for {type(row).__name__} {row.pk}")
            return 0
        content = path.read_bytes()
        asset = register_generated_asset(
            content,
            owner=row.owner,
            organization=row.organization,
            file_name=path.name,
            kind=kind,
            content_type=content_type,
            provenance={
                "migrated_from": str(path.relative_to(_ROOT)),
                "row": f"{type(row).__name__}:{row.pk}",
                "sha256": hashlib.sha256(content).hexdigest(),
            },
        )
        setattr(row, link, asset)
        for field, value in clear.items():
            setattr(row, field, value)
        row.save(update_fields=[link, *clear])
        self.stdout.write(f"uploaded {path} -> asset {asset.id}")
        return 1
