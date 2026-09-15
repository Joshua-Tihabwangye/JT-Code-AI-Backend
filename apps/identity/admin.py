from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from apps.identity.models import User


@admin.register(User)
class JTCodeUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (
        (
            'Supabase',
            {
                'fields': (
                    'supabase_user_id', 'full_name', 'display_name', 'avatar_url',
                    'job_title', 'contact', 'country', 'timezone', 'bio',
                )
            },
        ),
    )
    list_display = ('supabase_user_id', 'email', 'full_name', 'is_active', 'is_staff')
    search_fields = ('supabase_user_id', 'email', 'full_name', 'display_name')
