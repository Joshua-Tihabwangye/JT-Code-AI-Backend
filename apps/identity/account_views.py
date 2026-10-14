"""The frontend ``/accounts/me/`` contract (profile, consents summary, plan, password).

Supabase Auth owns credentials and the e-mail address: a password change is
verified against Supabase (password grant) and applied through the Auth Admin
API; the e-mail address cannot be changed here.
"""

from __future__ import annotations

from typing import Any

import httpx
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from apps.core.throttling import BurstThrottle
from apps.core.views import APIView
from apps.identity.authorization import organization_for_request


class AccountUpdateSerializer(serializers.Serializer):
    firstName = serializers.CharField(max_length=120, required=False, allow_blank=True)
    lastName = serializers.CharField(max_length=120, required=False, allow_blank=True)
    email = serializers.EmailField(required=False)
    contact = serializers.CharField(max_length=100, required=False, allow_blank=True)
    countryCode = serializers.RegexField(r"^[A-Za-z]{2}$|^$", required=False, allow_blank=True)
    countryName = serializers.CharField(max_length=100, required=False, allow_blank=True)
    dialCode = serializers.RegexField(r"^\+?\d{1,4}$|^$", required=False, allow_blank=True)
    timezone = serializers.CharField(max_length=100, required=False, allow_blank=True)


class PasswordChangeSerializer(serializers.Serializer):
    currentPassword = serializers.CharField(max_length=256)
    newPassword = serializers.CharField(min_length=8, max_length=256)
    confirmPassword = serializers.CharField(max_length=256)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if attrs["newPassword"] != attrs["confirmPassword"]:
            mismatch = "Passwords do not match."
            raise serializers.ValidationError({"confirmPassword": mismatch})
        if attrs["newPassword"] == attrs["currentPassword"]:
            unchanged = "Choose a different password."
            raise serializers.ValidationError({"newPassword": unchanged})
        return attrs


AVATAR_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")
AVATAR_MAX_BYTES = 2 * 1024 * 1024
AVATAR_URL_TTL_SECONDS = 3600


def avatar_url_for(user: Any) -> str | None:
    """Uploaded avatars live in private storage, so hand out a short-lived signed URL."""
    asset = getattr(user, "avatar_asset", None)
    if asset is not None and asset.deleted_at is None and asset.storage_key:
        from apps.assets.supabase_storage import SupabaseStorageError, generate_signed_delivery_url

        try:
            return generate_signed_delivery_url(asset.storage_key, expires_in=AVATAR_URL_TTL_SECONDS)
        except SupabaseStorageError, httpx.HTTPError:
            return None
    return user.avatar_url or None


def serialize_account(user: Any, organization: Any) -> dict[str, Any]:
    from apps.governance.models import ConsentRecord
    from apps.usage.services import active_plan

    first, _, last = (user.full_name or "").strip().partition(" ")
    granted = set(
        ConsentRecord.objects.filter(user=user, status=ConsentRecord.Status.GRANTED).values_list(
            "consent_type", flat=True
        )
    )
    plan = active_plan(organization) if organization is not None else None
    return {
        "id": str(user.id),
        "email": user.email,
        "firstName": first,
        "lastName": last,
        "contact": user.contact,
        "countryCode": user.country_code,
        "countryName": user.country,
        "dialCode": user.dial_code,
        "timezone": user.timezone,
        "avatarUrl": avatar_url_for(user),
        "createdAt": user.created_at.isoformat(),
        "termsAccepted": ConsentRecord.ConsentType.TERMS in granted,
        "privacyAccepted": ConsentRecord.ConsentType.PRIVACY in granted,
        "plan": getattr(plan, "slug", None) or "free",
    }


class AccountView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: OpenApiTypes.OBJECT})
    def get(self, request: Request) -> Response:
        return Response(serialize_account(request.user, organization_for_request(request)))

    @extend_schema(request=AccountUpdateSerializer, responses={200: OpenApiTypes.OBJECT})
    def patch(self, request: Request) -> Response:
        serializer = AccountUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        user = request.user
        if "email" in data and data["email"].lower() != (user.email or "").lower():
            return Response(
                {"email": ["Change your e-mail address through sign-in settings (Supabase Auth)."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        first, _, last = (user.full_name or "").partition(" ")
        if "firstName" in data or "lastName" in data:
            user.full_name = f"{data.get('firstName', first)} {data.get('lastName', last)}".strip()
        mapping = {
            "contact": "contact",
            "countryCode": "country_code",
            "countryName": "country",
            "dialCode": "dial_code",
            "timezone": "timezone",
        }
        for key, field in mapping.items():
            if key in data:
                value = data[key].upper() if key == "countryCode" else data[key]
                setattr(user, field, value)
        user.save()
        return Response(serialize_account(user, organization_for_request(request)))


class PasswordChangeView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [BurstThrottle]

    @extend_schema(request=PasswordChangeSerializer, responses={204: None})
    def post(self, request: Request) -> Response:
        from apps.identity.supabase_admin import SupabaseAdminError, set_password, verify_password

        serializer = PasswordChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            if not verify_password(request.user.email, data["currentPassword"]):
                return Response(
                    {"currentPassword": ["The current password is incorrect."]},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            set_password(request.user.supabase_user_id, data["newPassword"])
        except ValueError as exc:
            return Response({"newPassword": [str(exc)]}, status=status.HTTP_400_BAD_REQUEST)
        except SupabaseAdminError:
            return Response(
                {"detail": "The identity provider is unavailable; try again shortly."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(status=status.HTTP_204_NO_CONTENT)


class AccountAvatarView(APIView):
    """Upload (multipart ``file``) or remove the signed-in user's avatar."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [BurstThrottle]

    @extend_schema(request=OpenApiTypes.BINARY, responses={200: OpenApiTypes.OBJECT})
    def post(self, request: Request) -> Response:
        from apps.assets.services import register_generated_asset
        from apps.assets.supabase_storage import SupabaseStorageError, content_matches_type

        upload = request.FILES.get("file")
        if upload is None:
            return Response(
                {"file": ["Attach an image as the 'file' field."]}, status=status.HTTP_400_BAD_REQUEST
            )
        content_type = (upload.content_type or "").split(";", 1)[0].strip().lower()
        if content_type not in AVATAR_TYPES:
            return Response(
                {"file": ["Use a PNG, JPEG, WebP or GIF image."]}, status=status.HTTP_400_BAD_REQUEST
            )
        if upload.size is None or upload.size > AVATAR_MAX_BYTES:
            return Response(
                {"file": ["Avatars are limited to 2 MB."]}, status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
            )
        content = upload.read()
        if not content_matches_type(content[:64], content_type):
            return Response(
                {"file": ["The file content does not match its image type."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        organization = organization_for_request(request, required=True)
        user = request.user
        try:
            asset = register_generated_asset(
                content,
                owner=user,
                organization=organization,
                file_name=f"avatar.{content_type.rsplit('/', 1)[-1]}",
                kind="avatars",
                content_type=content_type,
                provenance={"origin": "avatar"},
            )
        except SupabaseStorageError, httpx.HTTPError:
            return Response(
                {"detail": "File storage is unavailable; try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        user.avatar_asset = asset
        user.avatar_url = ""
        user.save(update_fields=["avatar_asset", "avatar_url"])
        return Response(serialize_account(user, organization))

    @extend_schema(request=None, responses={200: OpenApiTypes.OBJECT})
    def delete(self, request: Request) -> Response:
        user = request.user
        user.avatar_asset = None
        user.avatar_url = ""
        user.save(update_fields=["avatar_asset", "avatar_url"])
        return Response(serialize_account(user, organization_for_request(request)))
