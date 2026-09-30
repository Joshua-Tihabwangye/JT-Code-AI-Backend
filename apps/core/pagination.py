from rest_framework.pagination import CursorPagination


class CreatedCursorPagination(CursorPagination):
    """Stable cursor pagination for append-only, high-volume API resources."""

    page_size = 50
    page_size_query_param = "pageSize"
    max_page_size = 100
    ordering = ("-created_at", "-id")


class UpdatedCursorPagination(CreatedCursorPagination):
    """Stable cursor pagination for resources whose latest update is most useful."""

    ordering = ("-updated_at", "-id")
