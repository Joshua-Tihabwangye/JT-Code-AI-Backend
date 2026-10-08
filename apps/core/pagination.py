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


class OptionalPaginationMixin:
    """Paginate only when the client asks (``cursor`` or ``pageSize``); otherwise return
    a plain JSON array of at most ``unpaginated_limit`` items - the frontend contract."""

    unpaginated_limit = 200

    def paginate_queryset(self, queryset, request, view=None):  # type: ignore[no-untyped-def]
        if "cursor" not in request.query_params and "pageSize" not in request.query_params:
            self._plain = True
            return list(queryset[: self.unpaginated_limit])
        self._plain = False
        return super().paginate_queryset(queryset, request, view)  # type: ignore[misc]

    def get_paginated_response(self, data):  # type: ignore[no-untyped-def]
        if getattr(self, "_plain", False):
            from rest_framework.response import Response

            return Response(data)
        return super().get_paginated_response(data)  # type: ignore[misc]


class OptionalUpdatedCursorPagination(OptionalPaginationMixin, UpdatedCursorPagination):
    pass


class OptionalCreatedCursorPagination(OptionalPaginationMixin, CreatedCursorPagination):
    pass


class _ExportSettings:
    URL_FORMAT_OVERRIDE = None

    def __getattr__(self, name):  # type: ignore[no-untyped-def]
        from rest_framework.settings import api_settings

        return getattr(api_settings, name)


class ExportFormatMixin:
    """``?format=`` is the *export* format on ``export`` actions (the frontend contract),
    not DRF's renderer override (``URL_FORMAT_OVERRIDE``), which would 404 on ``md``."""

    def get_content_negotiator(self):  # type: ignore[no-untyped-def]
        negotiator = super().get_content_negotiator()  # type: ignore[misc]
        if getattr(self, "action", None) == "export":
            negotiator.settings = _ExportSettings()  # per-request instance; no global state
        return negotiator
