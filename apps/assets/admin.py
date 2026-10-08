from django.contrib import admin

from apps.assets.models import Asset, UploadIntent


@admin.register(Asset)
class AssetAdmin(admin.ModelAdmin):
    list_display = ("id", "owner", "resource_type", "format", "bytes", "status", "created_at")
    list_filter = ("resource_type", "status", "created_at")
    search_fields = ("owner__email", "storage_object_id", "storage_key", "original_filename")
    readonly_fields = ("id", "created_at", "updated_at")
    ordering = ("-created_at",)


@admin.register(UploadIntent)
class UploadIntentAdmin(admin.ModelAdmin):
    list_display = ("id", "owner", "organization", "file_name", "status", "expires_at")
    list_filter = ("status", "created_at", "expires_at")
    search_fields = ("file_name", "original_filename", "owner__email", "storage_object_key")
    readonly_fields = ("id", "token", "created_at", "completed_at")
    raw_id_fields = ("owner", "organization")
