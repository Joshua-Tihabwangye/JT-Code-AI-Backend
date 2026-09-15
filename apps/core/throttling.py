from __future__ import annotations

from rest_framework.throttling import SimpleRateThrottle


class PerUserRateThrottle(SimpleRateThrottle):
    """Rate throttle keyed by authenticated user instead of IP."""

    def get_cache_key(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return None
        return self.cache_format % {'scope': self.scope, 'ident': f'user:{request.user.id}'}


class ChatThrottle(PerUserRateThrottle):
    scope = 'chat'


class ImageThrottle(PerUserRateThrottle):
    scope = 'images'


class EmbeddingThrottle(PerUserRateThrottle):
    scope = 'embeddings'


class ConversionThrottle(PerUserRateThrottle):
    scope = 'conversions'


class ResearchThrottle(PerUserRateThrottle):
    scope = 'research'


class BurstThrottle(PerUserRateThrottle):
    scope = 'burst'
