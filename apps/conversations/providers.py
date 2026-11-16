from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings


class AIProviderNotConfigured(RuntimeError):
    pass


class TextProvider:
    def generate(self, prompt: str) -> str:
        raise NotImplementedError


@dataclass
class EchoDevelopmentProvider(TextProvider):
    prefix: str = "Development mode: no AI model is connected yet, so this is a placeholder reply."

    def generate(self, prompt: str) -> str:
        return f'{self.prefix} You said: "{prompt.strip()}"'


class DisabledProvider(TextProvider):
    def generate(self, prompt: str) -> str:
        raise AIProviderNotConfigured("No AI provider adapter is configured for JT-Code.")


def get_text_provider() -> TextProvider:
    if settings.AI_PROVIDER == "echo" and settings.DEBUG:
        return EchoDevelopmentProvider()
    return DisabledProvider()
