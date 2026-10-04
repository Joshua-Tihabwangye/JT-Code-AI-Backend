"""Migrate pre-ImageKit asset references into the ImageKit registry.

Two sources of legacy references exist:

1. ``Asset`` rows quarantined by ``assets.0007`` because they predate ImageKit
   identities. Map each to the ImageKit file that now holds its bytes::

       python manage.py migrate_legacy_assets --map <asset-uuid>=<imagekit-file-id>

   The file is verified through the ImageKit API (path, size, privacy) and its
   bytes are hashed before the row becomes READY again.

2. Files written by the development-only local fallback (generated images,
   rendered documents, conversion outputs). ``--local-fallbacks`` uploads each
   file privately to ImageKit, links the new ``Asset`` to its row and clears the
   local URL.

Use ``--dry-run`` to list what would change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.assets.imagekit import (
    FINGERPRINT_VERSION,
    content_checksum,
    imagekit_is_configured,
    provider_identity_fingerprint,
    verify_imagekit_file,
)
from apps.assets.models import Asset
from apps.assets.services import register_generated_asset

_ROOT = Path(settings.BASE_DIR)
_DOCUMENT_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


class Command(BaseCommand):
    help = "Map legacy asset rows to ImageKit files and upload local-fallback files to ImageKit."

    def add_arguments(self, parser):
        parser.add_argument("--map", action="append", default=[], metavar="ASSET_ID=FILE_ID")
        parser.add_argument("--local-fallbacks", action="store_true")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: Any, **options: Any) -> None:
        if not imagekit_is_configured() and not options["dry_run"]:
            raise CommandError("ImageKit must be configured to migrate assets.")
        mapped = sum(self._map(pair, dry_run=options["dry_run"]) for pair in options["map"])
        uploaded = self._local_fallbacks(dry_run=options["dry_run"]) if options["local_fallbacks"] else 0
        self.stdout.write(self.style.SUCCESS(f"mapped={mapped} uploaded={uploaded}"))

    def _map(self, pair: str, *, dry_run: bool) -> int:
        asset_id, _, file_id = pair.partition("=")
        asset = Asset.objects.filter(id=asset_id).first()
        if asset is None or not file_id:
            raise CommandError(f"Invalid mapping {pair!r}: expected <asset-uuid>=<imagekit-file-id>.")
        if dry_run:
            self.stdout.write(f"would map {asset.id} -> {file_id}")
            return 0
        resource = verify_imagekit_file(file_id)
        if resource.get("isPrivateFile") is not True:
            raise CommandError(f"ImageKit file {file_id} must be private before it is registered.")
        size = int(resource["size"])
        checksum, _content_type, _head = content_checksum(resource["filePath"], expected_size=size)
        with transaction.atomic():
            asset.imagekit_file_id = file_id
            asset.imagekit_file_path = resource["filePath"]
            asset.secure_url = resource["url"]
            asset.resource_type = resource.get("fileType") or asset.resource_type
            asset.bytes = size
            asset.checksum_sha256 = checksum
            asset.provider_fingerprint = provider_identity_fingerprint(resource)
            asset.status = Asset.Status.READY
            asset.deletion_error = ""
            asset.last_verified_at = timezone.now()
            asset.provenance = {
                **(asset.provenance or {}),
                "provider": "imagekit",
                "migration_required": False,
                "migrated_at": timezone.now().isoformat(),
                "fingerprintVersion": FINGERPRINT_VERSION,
            }
            asset.save()
        self.stdout.write(f"mapped {asset.id} -> {file_id}")
        return 1

    def _local_fallbacks(self, *, dry_run: bool) -> int:
        from apps.ai_gateway.models import GeneratedImage
        from apps.conversions.models import ConversionJob
        from apps.documents.models import Document

        uploaded = 0
        for image in GeneratedImage.objects.filter(asset__isnull=True).select_related(
            "organization", "owner"
        ):
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
        asset = register_generated_asset(
            path.read_bytes(),
            owner=row.owner,
            organization=row.organization,
            file_name=path.name,
            kind=kind,
            content_type=content_type,
            provenance={
                "migrated_from": str(path.relative_to(_ROOT)),
                "row": f"{type(row).__name__}:{row.pk}",
            },
        )
        setattr(row, link, asset)
        for field, value in clear.items():
            setattr(row, field, value)
        row.save(update_fields=[link, *clear])
        self.stdout.write(f"uploaded {path} -> asset {asset.id}")
        return 1
