"""Anthropic client helpers (no qgis imports). The API key is never logged."""
from __future__ import annotations

import importlib
import importlib.util

MODELS = (
    ("claude-opus-5-5", "Claude Opus 5.5 (default)"),
    ("claude-sonnet-5-5", "Claude Sonnet 5.5"),
    ("claude-haiku-5-5", "Claude Haiku 5.5"),
)
DEFAULT_MODEL = "claude-opus-5-5"
EFFORTS = ("low", "medium", "high")
DEFAULT_EFFORT = "medium"

# Server-side refusal fallback ("fallbacks": "default") is supported for these;
# Haiku 5.5 has none and must not be sent the parameter.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5")


def anthropic_available() -> bool:
    try:
        if importlib.util.find_spec("anthropic") is None:
            return False
        importlib.import_module("anthropic")
        return True
    except Exception:
        return False


def make_client(api_key: str):
    import anthropic

    return anthropic.Anthropic(api_key=api_key, max_retries=2,
                               timeout=anthropic.Timeout(600.0, connect=15.0))


def friendly_error(exc: BaseException) -> str:
    """Map SDK exceptions to short user-facing text (never includes the key)."""
    try:
        import anthropic
    except Exception:
        return f"Unexpected error: {type(exc).__name__}: {exc}"
    A = anthropic
    if isinstance(exc, A.AuthenticationError):
        return "Authentication failed: the API key was rejected. Check it in WWNet > AI settings."
    if isinstance(exc, A.PermissionDeniedError):
        return "Permission denied: this API key cannot use the selected model."
    if isinstance(exc, A.RateLimitError):
        retry = "60"
        try:
            retry = exc.response.headers.get("retry-after", "60")
        except Exception:
            pass
        return f"Rate limited by the API. Try again in about {retry} s."
    if isinstance(exc, A.NotFoundError):
        return "Model or endpoint not found. Check the model id in settings."
    if isinstance(exc, A.BadRequestError):
        return f"The API rejected the request: {getattr(exc, 'message', exc)}"
    if isinstance(exc, A.APIConnectionError):  # includes APITimeoutError
        return "Network error: could not reach api.anthropic.com. Check your connection/proxy."
    if isinstance(exc, A.APIStatusError):
        if exc.status_code >= 500:
            return f"Anthropic API server error ({exc.status_code}). Try again shortly."
        return f"API error ({exc.status_code}): {getattr(exc, 'message', exc)}"
    return f"Unexpected error: {type(exc).__name__}: {exc}"


def test_connection(api_key: str, model: str):
    """Tiny request to verify key + model. Returns (ok, message)."""
    if not anthropic_available():
        return False, "The `anthropic` Python package is not installed."
    if not api_key:
        return False, "No API key entered."
    try:
        client = make_client(api_key)
        msg = client.messages.create(
            model=model, max_tokens=256,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": "Reply with the single word: ok"}],
        )
        return True, f"Connected. Model {getattr(msg, 'model', model)} responded."
    except Exception as exc:  # noqa: BLE001 - mapped to friendly text
        return False, friendly_error(exc)
