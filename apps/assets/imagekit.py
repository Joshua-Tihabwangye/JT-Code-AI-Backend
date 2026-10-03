from __future__ import annotations

import hashlib
import hmac
import io
import re
import time
import uuid
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx
from django.conf import settings

IMAGEKIT_UPLOAD_URL = "https://upload.imagekit.io/api/v1/files/upload"
IMAGEKIT_API_URL = "https://api.imagekit.io/v1"


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


def sanitize_file_name(file_name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", file_name.strip())
    return cleaned.strip("._") or "upload"


def user_upload_folder(user) -> str:
    root = settings.IMAGEKIT_UPLOAD_FOLDER.strip("/") or "jt-code"
    return f"/{root}/{user.id}"


def generate_upload_auth(*, token: str | None = None, expire: int | None = None) -> dict[str, Any]:
    token = token or str(uuid.uuid4())
    expire = expire or current_timestamp() + settings.IMAGEKIT_UPLOAD_AUTH_TTL_SECONDS
    payload = f"{token}{expire}".encode()
    signature = hmac.new(
        settings.IMAGEKIT_PRIVATE_KEY.encode("utf-8"),
        payload,
        hashlib.sha1,
    ).hexdigest()
    return {"token": token, "expire": expire, "signature": signature}


def verify_imagekit_file(file_id: str) -> dict[str, Any]:
    response = httpx.get(
        f"{IMAGEKIT_API_URL}/files/{quote(file_id, safe='')}/details",
        auth=(settings.IMAGEKIT_PRIVATE_KEY, ""),
        timeout=settings.IMAGEKIT_API_TIMEOUT_SECONDS,
    )
    if response.status_code == 404:
        raise ImageKitNotFound(f"ImageKit file {file_id!r} was not found.")
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Invalid ImageKit file details payload.")
    if payload.get("fileId") != file_id or payload.get("type", "file") not in {"file", "file-version"}:
        raise ImageKitError("ImageKit returned a mismatched file identity.")
    return payload


def provider_identity_fingerprint(resource: dict[str, Any]) -> str:
    """Fingerprint provider identity; this is deliberately not a content hash."""
    canonical = "\x1f".join(
        str(resource.get(name) or "") for name in ("fileId", "filePath", "size", "updatedAt")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def content_checksum(file_path: str, *, expected_size: int) -> tuple[str, str]:
    """Download the verified object through a signed URL and hash its bytes."""
    if expected_size > settings.IMAGEKIT_MAX_UPLOAD_BYTES:
        raise ImageKitError("Provider object exceeds the configured upload limit.")
    digest = hashlib.sha256()
    received = 0
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
            digest.update(block)
    if received != expected_size:
        raise ImageKitError("Provider download size does not match verified metadata.")
    return digest.hexdigest(), content_type


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


def delete_imagekit_file(file_id: str) -> None:
    response = httpx.delete(
        f"{IMAGEKIT_API_URL}/files/{quote(file_id, safe='')}",
        auth=(settings.IMAGEKIT_PRIVATE_KEY, ""),
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
    if not imagekit_is_configured():
        raise ImageKitError("ImageKit is not configured.")
    if not content or len(content) > settings.IMAGEKIT_MAX_UPLOAD_BYTES:
        raise ImageKitError("Generated asset is empty or exceeds the configured upload limit.")
    response = httpx.post(
        IMAGEKIT_UPLOAD_URL,
        auth=(settings.IMAGEKIT_PRIVATE_KEY, ""),
        data={
            "fileName": sanitize_file_name(file_name),
            "folder": folder,
            "useUniqueFileName": "false",
            "overwriteFile": "true",
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


def upload_bytes_to_imagekit(
    content: bytes,
    *,
    file_name: str,
    folder: str,
    content_type: str = "application/octet-stream",
) -> str | None:
    """Compatibility wrapper for optional development upload fallbacks."""
    try:
        return upload_bytes_to_imagekit_details(
            content, file_name=file_name, folder=folder, content_type=content_type
        )["url"]
    except Exception:
        return None


def list_imagekit_files(*, path: str, skip: int = 0, limit: int = 100) -> list[dict[str, Any]]:
    response = httpx.get(
        f"{IMAGEKIT_API_URL}/files",
        params={"path": path, "type": "file", "skip": skip, "limit": min(limit, 1000)},
        auth=(settings.IMAGEKIT_PRIVATE_KEY, ""),
        timeout=settings.IMAGEKIT_API_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise ImageKitError("ImageKit file listing returned an invalid response.")
    return payload


def provider_created_at(resource: dict[str, Any]) -> datetime | None:
    value = resource.get("createdAt")
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
