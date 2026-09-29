from drf_spectacular.extensions import OpenApiAuthenticationExtension


class SupabaseJWTAuthenticationScheme(OpenApiAuthenticationExtension):
    """Describe the project authentication class once for every v1 operation."""

    target_class = "apps.identity.authentication.SupabaseJWTAuthentication"
    name = "SupabaseBearer"
    priority = 1

    def get_security_definition(self, auto_schema):
        return {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": "Supabase access token.",
        }
