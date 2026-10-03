"""Relational authorization policy for tenant analytics records."""

from __future__ import annotations

from django.db.models import Q, QuerySet

from apps.analytics.models import AnalysisRun, Dataset, DatasetGrant, Visualization
from apps.identity.authorization import user_has_role
from apps.identity.models import Role


def _is_admin(user, organization) -> bool:
    return user_has_role(user, Role.RoleType.ADMIN, organization.id)


def datasets_visible_to(user, organization) -> QuerySet[Dataset]:
    queryset = Dataset.objects.filter(organization=organization)
    if _is_admin(user, organization):
        return queryset
    return queryset.filter(Q(owner=user) | Q(is_shared=True) | Q(grants__user=user)).distinct()


def datasets_analyzable_by(user, organization) -> QuerySet[Dataset]:
    queryset = Dataset.objects.filter(organization=organization)
    if _is_admin(user, organization):
        return queryset
    return queryset.filter(
        Q(owner=user)
        | Q(is_shared=True)
        | Q(grants__user=user, grants__permission=DatasetGrant.Permission.ANALYZE)
    ).distinct()


def can_manage_dataset(user, dataset: Dataset) -> bool:
    return dataset.owner_id == user.id or _is_admin(user, dataset.organization)


def datasets_manageable_by(user, organization) -> QuerySet[Dataset]:
    queryset = Dataset.objects.filter(organization=organization)
    return queryset if _is_admin(user, organization) else queryset.filter(owner=user)


def analysis_runs_visible_to(user, organization) -> QuerySet[AnalysisRun]:
    queryset = AnalysisRun.objects.filter(dataset__in=datasets_visible_to(user, organization))
    if _is_admin(user, organization):
        return queryset
    # Results belong to the person who executed them and to the dataset owner.
    return queryset.filter(Q(owner=user) | Q(dataset__owner=user)).distinct()


def visualizations_visible_to(user, organization) -> QuerySet[Visualization]:
    return Visualization.objects.filter(analysis_run__in=analysis_runs_visible_to(user, organization))
