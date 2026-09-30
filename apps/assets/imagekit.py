from __future__ import annotations

import hashlib
import hmac
import io
import re
import time
import uuid
from typing import Any

import httpx
from django.conf import settings

IMAGEKIT_UPLOAD_URL = "https://upload.imagekit.io/api/v1/files/upload"
IMAGEKIT_API_URL = "https://api.imagekit.io/v1"


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
    expire = expire or int(time.time()) + settings.IMAGEKIT_UPLOAD_AUTH_TTL_SECONDS
    payload = f"{token}{expire}".encode()
    signature = hmac.new(
        settings.IMAGEKIT_PRIVATE_KEY.encode("utf-8"),
        payload,
        hashlib.sha1,
    ).hexdigest()
    return {"token": token, "expire": expire, "signature": signature}


def verify_imagekit_file(file_id: str) -> dict[str, Any]:
    response = httpx.get(
        f"{IMAGEKIT_API_URL}/files/{file_id}/details",
        auth=(settings.IMAGEKIT_PRIVATE_KEY, ""),
        timeout=10.0,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Invalid ImageKit file details payload.")
    return payload


def upload_bytes_to_imagekit(
    content: bytes,
    *,
    file_name: str,
    folder: str,
    content_type: str = "application/octet-stream",
) -> str | None:
    if not imagekit_is_configured():
        return None
    try:
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
            timeout=30.0,
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            return payload.get("url") or None
    except Exception:
        return None
    return None
