from django.apps import AppConfig


class VisualizeConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.visualize"
    verbose_name = "Data visualization"

    def ready(self) -> None:
        # Registers the signal that drops a dataset's Postgres query table with it.
        from apps.visualize import store  # noqa: F401

        self._prewarm_engine()

    @staticmethod
    def _prewarm_engine() -> None:
        """API processes start the engine fork server now, so the first chart does not wait for it."""
        import os
        import threading

        from django.conf import settings

        if os.environ.get("JT_PREWARM_ENGINE") != "1" or not settings.ANALYTICS_SANDBOX_ZYGOTE:
            return

        def warm() -> None:
            from apps.analytics.sandbox import _zygote_for
            from apps.visualize.services import VIZ_ENGINE

            _zygote_for(VIZ_ENGINE)

        threading.Thread(target=warm, name="jt-engine-prewarm", daemon=True).start()
