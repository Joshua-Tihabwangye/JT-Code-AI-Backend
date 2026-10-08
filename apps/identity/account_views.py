"""The frontend ``/accounts/me/`` contract (profile, consents summary, plan, password).

Supabase Auth owns credentials and the e-mail address: a password change is
verified against Supabase (password grant) and applied through the Auth Admin
API; the e-mail address cannot be changed here.
"""

from __future__ import annotations

from typing import Any

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
        "avatarUrl": user.avatar_url or None,
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
