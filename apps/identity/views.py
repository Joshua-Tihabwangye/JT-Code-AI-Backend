import secrets
from datetime import timedelta

from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.generics import RetrieveAPIView, RetrieveUpdateAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.identity.authorization import organization_for_request, user_has_role
from apps.identity.models import OrganizationInvite, Role, UserOrganization, UserRole
from apps.identity.serializers import UserProfileSerializer, UserSerializer


class MeView(RetrieveAPIView):
    serializer_class = UserSerializer

    def get_object(self):
        return self.request.user


class SettingsProfileView(RetrieveUpdateAPIView):
    serializer_class = UserProfileSerializer
    permission_classes = [IsAuthenticated]

    def get_object(self):
        return self.request.user


class AuthPingView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        user = request.user
        return Response(
            {
                "authenticated": True,
                "userId": str(user.id),
                "supabaseUserId": user.supabase_user_id,
                "email": user.email,
            }
        )


def _admin_organization(request: Request):
    organization = organization_for_request(request, required=True)
    if not user_has_role(request.user, Role.RoleType.ADMIN, organization.id):
        raise PermissionDenied("Organization admin access is required.")
    return organization


def _member_payload(membership: UserOrganization, role_name: str) -> dict[str, str]:
    return {
        "user_id": str(membership.user_id),
        "email": membership.user.email,
        "role": role_name,
    }


class OrganizationMembersView(APIView):
    """List members and create time-limited invitations for the selected tenant."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        organization = _admin_organization(request)
        roles = {
            str(role.user_id): role.role.name
            for role in UserRole.objects.filter(organization=organization).select_related("role")
        }
        memberships = UserOrganization.objects.filter(organization=organization).select_related("user")
        return Response(
            {
                "members": [
                    _member_payload(member, roles.get(str(member.user_id), Role.RoleType.VIEWER))
                    for member in memberships
                ]
            }
        )

    def post(self, request: Request) -> Response:
        organization = _admin_organization(request)
        email = str(request.data.get("email", "")).strip().lower()
        role_name = str(request.data.get("role", Role.RoleType.VIEWER)).lower()
        if not email or role_name not in Role.RoleType.values:
            return Response(
                {"detail": "A valid email and role (admin, editor, or viewer) are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if UserOrganization.objects.filter(organization=organization, user__email__iexact=email).exists():
            return Response(
                {"detail": "This user is already an organization member."},
                status=status.HTTP_409_CONFLICT,
            )
        invite = OrganizationInvite.objects.create(
            organization=organization,
            email=email,
            role=role_name,
            token=secrets.token_urlsafe(32),
            expires_at=timezone.now() + timedelta(days=7),
        )
        return Response(
            {
                "id": str(invite.id),
                "email": invite.email,
                "role": invite.role,
                "token": invite.token,
                "expires_at": invite.expires_at.isoformat(),
            },
            status=status.HTTP_201_CREATED,
        )


class OrganizationMemberDetailView(APIView):
    """Change a non-owner member's role or remove that member from the tenant."""

    permission_classes = [IsAuthenticated]

    def patch(self, request: Request, user_id) -> Response:
        organization = _admin_organization(request)
        if str(organization.owner_id) == str(user_id):
            return Response(
                {"detail": "The organization owner role cannot be changed here."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        role_name = str(request.data.get("role", "")).lower()
        if role_name not in Role.RoleType.values:
            return Response(
                {"detail": "role must be admin, editor, or viewer."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        membership = (
            UserOrganization.objects.filter(organization=organization, user_id=user_id)
            .select_related("user")
            .first()
        )
        if membership is None:
            return Response({"detail": "Member not found."}, status=status.HTTP_404_NOT_FOUND)
        role, _ = Role.objects.get_or_create(name=role_name)
        UserRole.objects.filter(user=membership.user, organization=organization).exclude(role=role).delete()
        UserRole.objects.get_or_create(user=membership.user, organization=organization, role=role)
        return Response(_member_payload(membership, role_name))

    def delete(self, request: Request, user_id) -> Response:
        organization = _admin_organization(request)
        if str(organization.owner_id) == str(user_id):
            return Response(
                {"detail": "The organization owner cannot be removed."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        deleted, _ = UserOrganization.objects.filter(organization=organization, user_id=user_id).delete()
        if not deleted:
            return Response({"detail": "Member not found."}, status=status.HTTP_404_NOT_FOUND)
        UserRole.objects.filter(organization=organization, user_id=user_id).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class OrganizationInviteAcceptView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request: Request, invite_id) -> Response:
        invite = OrganizationInvite.objects.select_related("organization").filter(id=invite_id).first()
        if invite is None or invite.status != OrganizationInvite.Status.PENDING:
            return Response(
                {"detail": "Invite not found or no longer valid."}, status=status.HTTP_404_NOT_FOUND
            )
        if invite.expires_at and invite.expires_at <= timezone.now():
            invite.status = OrganizationInvite.Status.EXPIRED
            invite.save(update_fields=["status"])
            return Response({"detail": "Invite has expired."}, status=status.HTTP_410_GONE)
        if request.user.email.lower() != invite.email.lower():
            raise PermissionDenied("This invite was issued to a different email address.")
        membership, _ = UserOrganization.objects.get_or_create(
            user=request.user, organization=invite.organization
        )
        role, _ = Role.objects.get_or_create(name=invite.role)
        UserRole.objects.filter(user=request.user, organization=invite.organization).exclude(
            role=role
        ).delete()
        UserRole.objects.get_or_create(user=request.user, organization=invite.organization, role=role)
        invite.status = OrganizationInvite.Status.ACCEPTED
        invite.save(update_fields=["status"])
        return Response(_member_payload(membership, role.name))
