"""URL and deduplication helpers."""

from __future__ import annotations

import re
from urllib.parse import urlparse


def normalize_url(url: str) -> str:
    value = (url or "").strip()
    value = re.sub(r"^https?://", "", value, flags=re.IGNORECASE)
    value = value.rstrip("/")
    return value.lower()


def is_valid_http_url(url: str) -> bool:
    try:
        parsed = urlparse((url or "").strip())
    except Exception:
        return False

    if parsed.scheme not in {"http", "https"}:
        return False
    if not parsed.netloc:
        return False
    return True


def dedupe_key(title: str, url: str) -> tuple[str, str]:
    return (normalize_url(url), (title or "").strip().lower())
