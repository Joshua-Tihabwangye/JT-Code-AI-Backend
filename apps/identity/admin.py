from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from apps.identity.models import Organization, OrganizationInvite, Role, User, UserPermission, UserRole


@admin.register(User)
class JTCodeUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (
        (
            "Supabase",
            {
                "fields": (
                    "supabase_user_id",
                    "full_name",
                    "display_name",
                    "avatar_url",
                    "job_title",
                    "contact",
                    "country",
                    "timezone",
                    "bio",
                )
            },
        ),
    )
    list_display = ("supabase_user_id", "email", "full_name", "is_active", "is_staff")
    search_fields = ("supabase_user_id", "email", "full_name", "display_name")


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ("name", "description")
    search_fields = ("name", "description")


@admin.register(UserRole)
class UserRoleAdmin(admin.ModelAdmin):
    list_display = ("user", "role", "organization", "assigned_at")
    list_filter = ("role", "organization")
    search_fields = ("user__email", "user__supabase_user_id")
    raw_id_fields = ("user", "organization")


@admin.register(UserPermission)
class UserPermissionAdmin(admin.ModelAdmin):
    list_display = ("user", "permission", "organization", "granted_at")
    list_filter = ("organization",)
    search_fields = ("user__email", "permission__codename")
    raw_id_fields = ("user", "organization")


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "owner", "created_at")
    search_fields = ("name", "slug", "owner__email")
    raw_id_fields = ("owner",)


@admin.register(OrganizationInvite)
class OrganizationInviteAdmin(admin.ModelAdmin):
    list_display = ("email", "organization", "role", "status", "expires_at")
    list_filter = ("role", "status")
    search_fields = ("email", "organization__name")
    raw_id_fields = ("organization",)
