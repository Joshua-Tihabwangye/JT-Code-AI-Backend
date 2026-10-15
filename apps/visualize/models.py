from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class SavedChart(models.Model):
    """A chart configuration plus a snapshot of its rendered Apache ECharts option."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "identity.Organization", on_delete=models.CASCADE, related_name="saved_charts"
    )
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="saved_charts")
    dataset = models.ForeignKey(
        "analytics.Dataset", null=True, blank=True, on_delete=models.SET_NULL, related_name="saved_charts"
    )
    title = models.CharField(max_length=200)
    chart_type = models.CharField(max_length=64)
    # {"fields": {...}, "options": {...}, "style": {...}} validated against the catalog.
    config = models.JSONField(default=dict, blank=True)
    option = models.JSONField(default=dict, blank=True)
    # {"about", "howToRead", "insight"}: the plain-language explanation shown with the chart.
    explanation = models.JSONField(default=dict, blank=True)
    stats = models.JSONField(default=list, blank=True)
    notes = models.JSONField(default=list, blank=True)
    description = models.TextField(blank=True)
    is_shared = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=("organization", "owner", "-updated_at"), name="visualize_chart_owner_idx")
        ]

    def __str__(self) -> str:
        return self.title


class Dashboard(models.Model):
    """A Power BI-style dashboard: pages of visuals, filters, parameters and a theme, as one JSON spec.

    The spec is validated by ``apps.visualize.dashboard_spec``; ``version`` increases on every save
    so concurrent editors are detected, and ``DashboardVersion`` keeps restorable snapshots.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "identity.Organization", on_delete=models.CASCADE, related_name="viz_dashboards"
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="viz_dashboards"
    )
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    spec = models.JSONField(default=dict, blank=True)
    spec_version = models.PositiveSmallIntegerField(default=1)
    # A small PNG data URL of page 1 for the dashboards home (rendered in the browser).
    thumbnail = models.TextField(blank=True)
    is_shared = models.BooleanField(default=False)
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=("organization", "owner", "-updated_at"), name="visualize_dash_owner_idx")
        ]

    def __str__(self) -> str:
        return self.name


class DashboardVersion(models.Model):
    """A restorable snapshot of a dashboard spec."""

    dashboard = models.ForeignKey(Dashboard, on_delete=models.CASCADE, related_name="versions")
    number = models.PositiveIntegerField()
    name = models.CharField(max_length=200, blank=True)
    spec = models.JSONField(default=dict)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-number",)
        constraints = [
            models.UniqueConstraint(fields=("dashboard", "number"), name="visualize_dash_version_unique")
        ]

    def __str__(self) -> str:
        return f"{self.dashboard_id} v{self.number}"


class DashboardMember(models.Model):
    """Per-dashboard sharing: a viewer can look and personalize, an editor can also change it."""

    class Role(models.TextChoices):
        VIEWER = "viewer", "Viewer"
        EDITOR = "editor", "Editor"

    dashboard = models.ForeignKey(Dashboard, on_delete=models.CASCADE, related_name="members")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    role = models.CharField(max_length=8, choices=Role.choices, default=Role.VIEWER)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("dashboard", "user"), name="visualize_dash_member_unique")
        ]

    def __str__(self) -> str:
        return f"{self.user_id} ({self.role})"


class PersonalView(models.Model):
    """A viewer's own changes to a dashboard (visual overrides, slicer and filter state).

    It never changes the author's spec; resetting simply deletes it.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    dashboard = models.ForeignKey(Dashboard, on_delete=models.CASCADE, related_name="personal_views")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    name = models.CharField(max_length=120, default="My view")
    state = models.JSONField(default=dict, blank=True)
    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=("dashboard", "user"), name="visualize_pview_user_idx")]

    def __str__(self) -> str:
        return self.name


class Theme(models.Model):
    """A custom dashboard theme (palette, canvas and tile colours, fonts, KPI style), as JSON."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "identity.Organization", on_delete=models.CASCADE, related_name="viz_themes"
    )
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="viz_themes")
    name = models.CharField(max_length=80)
    spec = models.JSONField(default=dict)
    is_shared = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.name


class Measure(models.Model):
    """A saved calculation (measures library): reusable by every dashboard in the organization."""

    class Format(models.TextChoices):
        NUMBER = "number", "Number"
        PERCENT = "percent", "Percent"
        CURRENCY = "currency", "Currency"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "identity.Organization", on_delete=models.CASCADE, related_name="viz_measures"
    )
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="viz_measures")
    # The dataset the measure was written for (its fields); empty = any dataset with those fields.
    dataset = models.ForeignKey(
        "analytics.Dataset", null=True, blank=True, on_delete=models.SET_NULL, related_name="viz_measures"
    )
    name = models.CharField(max_length=60)
    expression = models.TextField(max_length=1000)
    format = models.CharField(max_length=12, choices=Format.choices, default=Format.NUMBER)
    description = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class DatasetTable(models.Model):
    """The Postgres copy of a dataset used for fast filtered aggregation (``viz_data`` schema).

    The stored file remains the source of truth; this table is rebuilt whenever the dataset's
    content changes (``source_checksum``). Columns are stored as ``c0…cN``; ``columns`` maps them
    back to the dataset's names and kinds.
    """

    class Status(models.TextChoices):
        BUILDING = "building", "Building"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"

    dataset = models.OneToOneField(
        "analytics.Dataset", on_delete=models.CASCADE, primary_key=True, related_name="viz_table"
    )
    table_name = models.CharField(max_length=63, unique=True)
    columns = models.JSONField(default=list)
    row_count = models.PositiveIntegerField(default=0)
    source_checksum = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.BUILDING)
    error = models.CharField(max_length=300, blank=True)
    built_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.table_name
