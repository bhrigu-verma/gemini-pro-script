"""Gemini API client with UI-compatible ask/send/recv semantics."""

from __future__ import annotations

from dataclasses import dataclass, field
import importlib
from typing import Any, Optional

try:
    _tenacity = importlib.import_module("tenacity")
    retry = _tenacity.retry
    retry_if_exception_type = _tenacity.retry_if_exception_type
    stop_after_attempt = _tenacity.stop_after_attempt
    wait_exponential = _tenacity.wait_exponential
except Exception:  # pragma: no cover - allows module import before deps are installed
    def retry(*args, **kwargs):
        _ = (args, kwargs)

        def _decorator(func):
            return func

        return _decorator

    def retry_if_exception_type(*args, **kwargs):
        _ = (args, kwargs)
        return None

    def stop_after_attempt(*args, **kwargs):
        _ = (args, kwargs)
        return None

    def wait_exponential(*args, **kwargs):
        _ = (args, kwargs)
        return None

from config.constants import GEMINI_FALLBACK_MODELS, GEMINI_PRIMARY_MODEL
from config.defaults import (
    DEFAULT_API_BACKOFF_MAX_SECONDS,
    DEFAULT_API_BACKOFF_MIN_SECONDS,
    DEFAULT_API_MAX_RETRIES,
    DEFAULT_GEMINI_MAX_OUTPUT_TOKENS,
    DEFAULT_GEMINI_TEMPERATURE,
)

try:
    genai = importlib.import_module("google.genai")
    types = importlib.import_module("google.genai.types")
except Exception:  # pragma: no cover - import failure handled at runtime
    genai = None
    types = None


class GeminiAPIError(RuntimeError):
    """Raised for unrecoverable Gemini API failures."""


@dataclass
class GeminiAPIClient:
    api_key: str
    role_name: str = "API"
    primary_model: str = GEMINI_PRIMARY_MODEL
    fallback_models: list[str] = field(default_factory=lambda: list(GEMINI_FALLBACK_MODELS))
    temperature: float = DEFAULT_GEMINI_TEMPERATURE
    max_output_tokens: int = DEFAULT_GEMINI_MAX_OUTPUT_TOKENS
    max_retries: int = DEFAULT_API_MAX_RETRIES

    _pending_prompt: Optional[str] = None
    _client: Optional[Any] = None
    _last_model_used: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.api_key:
            raise GeminiAPIError("Missing Gemini API key")
        if genai is None or types is None:
            raise GeminiAPIError(
                "google-genai is not installed. Install dependencies before using API mode."
            )
        self._client = genai.Client(api_key=self.api_key)

    @property
    def model_chain(self) -> list[str]:
        return [self.primary_model, *self.fallback_models]

    @property
    def last_model_used(self) -> Optional[str]:
        return self._last_model_used

    def send(self, prompt: str) -> None:
        self._pending_prompt = prompt

    def recv(self) -> str:
        if not self._pending_prompt:
            raise GeminiAPIError("recv() called before send()")
        prompt = self._pending_prompt
        self._pending_prompt = None
        return self.ask(prompt)

    def send_prompt(self, prompt: str) -> None:
        self.send(prompt)

    def wait_response(self) -> str:
        return self.recv()

    def ask(self, prompt: str) -> str:
        errors: list[str] = []
        for model in self.model_chain:
            try:
                response_text = self._call_with_retry(prompt, model)
                self._last_model_used = model
                return response_text
            except Exception as exc:
                if self._is_rate_limited(exc):
                    errors.append(f"{model}: rate-limited ({exc})")
                    continue
                raise

        raise GeminiAPIError(
            "All configured models were exhausted due to quota/rate limit: " + " | ".join(errors)
        )

    def screenshot(self, path: str) -> None:
        _ = path

    def dump_dom(self, tag: str = "dom") -> None:
        _ = tag

    def probe(self) -> None:
        return

    @retry(
        stop=stop_after_attempt(DEFAULT_API_MAX_RETRIES),
        wait=wait_exponential(
            multiplier=1,
            min=DEFAULT_API_BACKOFF_MIN_SECONDS,
            max=DEFAULT_API_BACKOFF_MAX_SECONDS,
        ),
        retry=retry_if_exception_type((TimeoutError, ConnectionError)),
        reraise=True,
    )
    def _call_with_retry(self, prompt: str, model: str) -> str:
        if self._client is None:
            raise GeminiAPIError("API client is not initialized")

        config = types.GenerateContentConfig(
            temperature=self.temperature,
            max_output_tokens=self.max_output_tokens,
        )

        response = self._client.models.generate_content(
            model=model,
            contents=prompt,
            config=config,
        )

        text = getattr(response, "text", None)
        if isinstance(text, str) and text.strip():
            return text

        parts: list[str] = []
        candidates = getattr(response, "candidates", None) or []
        for candidate in candidates:
            content = getattr(candidate, "content", None)
            content_parts = getattr(content, "parts", None) or []
            for part in content_parts:
                part_text = getattr(part, "text", None)
                if part_text:
                    parts.append(part_text)

        if parts:
            return "\n".join(parts)

        raise GeminiAPIError("Gemini API returned no textual output")

    @staticmethod
    def _is_rate_limited(exc: Exception) -> bool:
        text = str(exc).lower()
        return (
            "429" in text
            or "quota" in text
            or "rate limit" in text
            or "resource_exhausted" in text
            or "too many requests" in text
        )


def build_stateless_round_payload(
    system_prompt: str,
    original_task: str,
    latest_draft: str,
    latest_critique: str,
) -> str:
    """Compose an isolated round payload to avoid context-window bloat."""
    return (
        f"System Instructions:\n{system_prompt}\n\n"
        f"Original Task:\n{original_task}\n\n"
        f"Latest Draft:\n{latest_draft}\n\n"
        f"Latest Critique:\n{latest_critique}\n\n"
        "Write a complete revised output that resolves the critique."
    )
