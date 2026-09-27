from django.contrib import admin

from apps.assets.models import Asset


@admin.register(Asset)
class AssetAdmin(admin.ModelAdmin):
    list_display = ("id", "owner", "resource_type", "format", "bytes", "status", "created_at")
    list_filter = ("resource_type", "status", "created_at")
    search_fields = ("owner__email", "imagekit_file_id", "imagekit_file_path", "original_filename")
    readonly_fields = ("id", "created_at", "updated_at")
    ordering = ("-created_at",)
