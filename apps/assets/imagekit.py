"""ImageKit REST client used by the asset registry.

* Browser uploads use ImageKit's V2 upload API with a server-issued JWT whose
  payload binds every upload parameter (folder, file name, privacy, overwrite
  and size check), so a client cannot redirect or widen an upload.
* Server-side uploads (generated artifacts, proxied uploads) use the private-key
  API and are always private files.
* Delivery is only through short-lived signed URLs.

Provider identity is fingerprinted from fields that change only when the
stored object changes (id, path, size, version), never from mutable metadata.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import re
import time
import uuid
from collections.abc import Iterator
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx
import jwt
from django.conf import settings

FINGERPRINT_VERSION = 2


class ImageKitError(RuntimeError):
    pass


class ImageKitNotFound(ImageKitError):
    pass


def current_timestamp() -> int:
    return int(time.time())


def imagekit_is_configured() -> bool:
    return (
        all(
            (
                settings.IMAGEKIT_PUBLIC_KEY,
                settings.IMAGEKIT_PRIVATE_KEY,
                settings.IMAGEKIT_ENDPOINT_URL,
            )
        )
        and settings.IMAGEKIT_PRIVATE_KEY != "replace_me"
    )


def _auth() -> tuple[str, str]:
    return (settings.IMAGEKIT_PRIVATE_KEY, "")


def _api(path: str) -> str:
    return f"{settings.IMAGEKIT_API_BASE.rstrip('/')}/{path.lstrip('/')}"


def upload_url(version: str = "v2") -> str:
    return f"{settings.IMAGEKIT_UPLOAD_API_BASE.rstrip('/')}/{version}/files/upload"


def sanitize_file_name(file_name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", file_name.strip())
    return cleaned.strip("._") or "upload"


def root_folder() -> str:
    return "/" + (settings.IMAGEKIT_UPLOAD_FOLDER.strip("/") or "jt-code")


def organization_folder(organization_id: Any, kind: str) -> str:
    """Tenant-scoped folder for one kind of asset (``uploads/<user>``, ``charts``...)."""
    return f"{root_folder()}/{organization_id}/{kind.strip('/')}"


def user_upload_folder(user: Any, organization_id: Any) -> str:
    return organization_folder(organization_id, f"uploads/{user.id}")


def generate_upload_token(params: dict[str, str], *, expires_at: int) -> str:
    """V2 upload JWT: the payload carries every upload parameter except ``file``."""
    now = current_timestamp()
    return jwt.encode(
        {**params, "iat": now, "exp": expires_at},
        settings.IMAGEKIT_PRIVATE_KEY,
        algorithm="HS256",
        headers={"kid": settings.IMAGEKIT_PUBLIC_KEY},
    )


def verify_imagekit_file(file_id: str) -> dict[str, Any]:
    response = httpx.get(
        _api(f"files/{quote(file_id, safe='')}/details"),
        auth=_auth(),
        timeout=settings.IMAGEKIT_API_TIMEOUT_SECONDS,
    )
    if response.status_code == 404:
        raise ImageKitNotFound(f"ImageKit file {file_id!r} was not found.")
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ImageKitError("Invalid ImageKit file details payload.")
    if payload.get("fileId") != file_id or payload.get("type", "file") not in {"file", "file-version"}:
        raise ImageKitError("ImageKit returned a mismatched file identity.")
    return payload


def provider_identity_fingerprint(resource: dict[str, Any]) -> str:
    """Fingerprint provider identity from immutable-per-version fields (not a content hash)."""
    version = (resource.get("versionInfo") or {}).get("id") or ""
    canonical = "\x1f".join(
        [
            str(resource.get("fileId") or ""),
            str(resource.get("filePath") or ""),
            str(resource.get("size") or ""),
            version,
        ]
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "application/pdf": (b"%PDF-",),
    "application/zip": (b"PK\x03\x04", b"PK\x05\x06"),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (b"PK\x03\x04",),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": (b"PK\x03\x04",),
}
_TEXT_TYPES = {"text/csv", "text/markdown", "text/plain", "application/json"}


def content_matches_type(head: bytes, content_type: str) -> bool:
    """True when the leading bytes are consistent with the declared content type."""
    content_type = content_type.split(";", 1)[0].strip().lower()
    if content_type == "image/webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if content_type in _SIGNATURES:
        return head.startswith(_SIGNATURES[content_type])
    if content_type in _TEXT_TYPES or content_type.startswith("text/"):
        if b"\x00" in head:
            return False
        try:
            head.decode("utf-8")
        except UnicodeDecodeError as exc:
            # A multi-byte character may be cut at the sniff boundary.
            return exc.start >= len(head) - 3
        return True
    return False


def validate_upload_type(content_type: str) -> str:
    normalized = content_type.split(";", 1)[0].strip().lower()
    if normalized not in settings.ASSET_ALLOWED_CONTENT_TYPES:
        raise ImageKitError("This content type is not allowed for asset uploads.")
    return normalized


def content_checksum(file_path: str, *, expected_size: int) -> tuple[str, str, bytes]:
    """Download the verified object via a signed URL; return (sha256, content type, first bytes)."""
    if expected_size > settings.IMAGEKIT_MAX_UPLOAD_BYTES:
        raise ImageKitError("Provider object exceeds the configured upload limit.")
    digest = hashlib.sha256()
    received = 0
    head = b""
    url = generate_signed_delivery_url(file_path)
    with httpx.stream(
        "GET", url, timeout=settings.IMAGEKIT_API_TIMEOUT_SECONDS, follow_redirects=False
    ) as response:
        response.raise_for_status()
        declared = response.headers.get("content-length")
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if declared and int(declared) != expected_size:
            raise ImageKitError("Provider download size does not match verified metadata.")
        for block in response.iter_bytes():
            received += len(block)
            if received > settings.IMAGEKIT_MAX_UPLOAD_BYTES:
                raise ImageKitError("Provider download exceeded the configured upload limit.")
            if len(head) < 512:
                head += block[: 512 - len(head)]
            digest.update(block)
    if received != expected_size:
        raise ImageKitError("Provider download size does not match verified metadata.")
    return digest.hexdigest(), content_type, head


def generate_signed_delivery_url(file_path: str, *, expires_in: int | None = None) -> str:
    """Generate ImageKit's short-lived `ik-t`/`ik-s` URL server-side."""
    path = "/" + file_path.lstrip("/")
    expiration = current_timestamp() + int(expires_in or settings.IMAGEKIT_SIGNED_URL_TTL_SECONDS)
    endpoint = settings.IMAGEKIT_ENDPOINT_URL.rstrip("/") + "/"
    relative_path = path.lstrip("/")
    signature = hmac.new(
        settings.IMAGEKIT_PRIVATE_KEY.encode("utf-8"),
        f"{relative_path}{expiration}".encode(),
        hashlib.sha1,
    ).hexdigest()
    return f"{endpoint}{quote(relative_path, safe='/')}?ik-t={expiration}&ik-s={signature}"


def stream_file(file_path: str, *, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
    """Stream a stored object's bytes through a signed URL (no redirects)."""
    with httpx.stream(
        "GET",
        generate_signed_delivery_url(file_path, expires_in=120),
        timeout=settings.IMAGEKIT_API_TIMEOUT_SECONDS,
        follow_redirects=False,
    ) as response:
        response.raise_for_status()
        yield from response.iter_bytes(chunk_size)


def delete_imagekit_file(file_id: str) -> None:
    response = httpx.delete(
        _api(f"files/{quote(file_id, safe='')}"),
        auth=_auth(),
        timeout=settings.IMAGEKIT_API_TIMEOUT_SECONDS,
    )
    if response.status_code not in {204, 404}:
        response.raise_for_status()


def upload_bytes_to_imagekit_details(
    content: bytes,
    *,
    file_name: str,
    folder: str,
    content_type: str = "application/octet-stream",
) -> dict[str, Any]:
    """Upload server-side bytes as a *private* file that never overwrites another."""
    if not imagekit_is_configured():
        raise ImageKitError("ImageKit is not configured.")
    if not content or len(content) > settings.IMAGEKIT_MAX_UPLOAD_BYTES:
        raise ImageKitError("Asset is empty or exceeds the configured upload limit.")
    response = httpx.post(
        upload_url("v1"),
        auth=_auth(),
        data={
            "fileName": sanitize_file_name(file_name),
            "folder": folder,
            "useUniqueFileName": "false",
            "overwriteFile": "false",
            "isPrivateFile": "true",
        },
        files={"file": (sanitize_file_name(file_name), io.BytesIO(content), content_type)},
        timeout=settings.IMAGEKIT_API_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()
    required = {"fileId", "filePath", "url", "size", "fileType"}
    if not isinstance(payload, dict) or not required.issubset(payload):
        raise ImageKitError("ImageKit upload returned an incomplete response.")
    if int(payload["size"]) != len(content):
        raise ImageKitError("ImageKit upload size does not match the submitted bytes.")
    return payload


def unique_file_name(file_name: str) -> str:
    """Prefix a random id so concurrent or repeated uploads never collide."""
    return f"{uuid.uuid4().hex[:12]}-{sanitize_file_name(file_name)}"


def list_imagekit_files(
    *, path: str, skip: int = 0, limit: int = 100, kind: str = "all"
) -> list[dict[str, Any]]:
    response = httpx.get(
        _api("files"),
        params={"path": path, "type": kind, "skip": skip, "limit": min(limit, 1000)},
        auth=_auth(),
        timeout=settings.IMAGEKIT_API_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise ImageKitError("ImageKit file listing returned an invalid response.")
    return payload


def walk_imagekit_files(
    root: str, *, max_depth: int, page_size: int, max_pages: int
) -> Iterator[dict[str, Any]]:
    """Yield every file under ``root`` by walking folders breadth-first (bounded)."""
    pending: list[tuple[str, int]] = [(root, 0)]
    while pending:
        folder, depth = pending.pop(0)
        for page in range(max_pages):
            items = list_imagekit_files(path=folder, skip=page * page_size, limit=page_size)
            for item in items:
                if item.get("type") == "folder":
                    child = str(item.get("folderPath") or f"{folder.rstrip('/')}/{item.get('name', '')}")
                    if depth + 1 <= max_depth:
                        pending.append((child, depth + 1))
                elif item.get("type", "file") == "file":
                    yield item
            if len(items) < page_size:
                break


def provider_created_at(resource: dict[str, Any]) -> datetime | None:
    value = resource.get("createdAt")
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
