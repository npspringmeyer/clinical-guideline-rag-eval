"""Small atomic cache for successful public-source HTTP responses."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import requests

from src.ingestion.storage import atomic_write_text

CACHE_TIMESTAMP_HEADER = "X-Local-Source-Retrieved-At"
CREDENTIAL_PARAMETER_NAMES = {"apikey", "api_key", "key", "token", "access_token"}


def _cache_path(cache_dir: Path, method: str, url: str, params: dict) -> Path:
    safe_params = {
        key: value
        for key, value in params.items()
        if key.lower() not in CREDENTIAL_PARAMETER_NAMES
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            [method.upper(), url, safe_params],
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    return cache_dir / f"{fingerprint}.json"


def _cached_response(
    path: Path, ttl_seconds: float, now: float
) -> requests.Response | None:
    if not path.is_file():
        return None
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
        retrieved = datetime.fromisoformat(envelope["retrieved_utc"]).timestamp()
        if not 0 <= now - retrieved <= ttl_seconds:
            return None
        content = base64.b64decode(envelope["content"], validate=True)
        if hashlib.sha256(content).hexdigest() != envelope["content_sha256"]:
            return None
        response = requests.Response()
        response.status_code = int(envelope["status_code"])
        if not 200 <= response.status_code < 300:
            return None
        response.headers.update(envelope.get("headers", {}))
        response.headers[CACHE_TIMESTAMP_HEADER] = envelope["retrieved_utc"]
        response._content = content
        response.url = envelope.get("url", "")
        return response
    except (OSError, ValueError, KeyError, TypeError):
        return None


def request_with_cache(
    request: Callable[[], requests.Response],
    *,
    method: str,
    url: str,
    params: dict,
    cache_dir: Path | None,
    ttl_seconds: float = 12 * 60 * 60,
    refresh: bool = False,
) -> requests.Response:
    """Return a recent successful response, or fetch and atomically cache it."""
    if ttl_seconds < 0:
        raise ValueError("cache TTL cannot be negative")
    path = (
        _cache_path(cache_dir, method, url, params)
        if cache_dir is not None
        else None
    )
    if path is not None and not refresh and ttl_seconds:
        cached = _cached_response(path, ttl_seconds, time.time())
        if cached is not None:
            return cached

    response = request()
    if path is None or response.status_code < 200 or response.status_code >= 300:
        return response

    retrieved_utc = datetime.now(timezone.utc).isoformat()
    envelope = {
        "status_code": response.status_code,
        "headers": {
            key: value
            for key, value in response.headers.items()
            if key.lower() in {"content-type", "etag", "last-modified", "retry-after"}
        },
        "url": url,
        "retrieved_utc": retrieved_utc,
        "content": base64.b64encode(response.content).decode("ascii"),
        "content_sha256": hashlib.sha256(response.content).hexdigest(),
    }
    atomic_write_text(
        path, json.dumps(envelope, ensure_ascii=False, sort_keys=True) + "\n"
    )
    response.headers[CACHE_TIMESTAMP_HEADER] = retrieved_utc
    return response
