from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import httpx

USER_AGENT = "subs/0.1"
MAX_BYTES = 20 * 1024 * 1024


class Network:
    def __init__(self, timeout: float = 12, cache: bool = True):
        self.timeout = timeout
        self.cache = cache
        self.cache_dir = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "easysubs"

    def fetch(self, url: str, *, params=None, headers=None) -> bytes:
        if not url.startswith(("https://", "http://")):
            raise ValueError("Only HTTP(S) download links are supported.")
        with httpx.Client(
            timeout=self.timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT, **(headers or {})}
        ) as client:
            with client.stream("GET", url, params=params) as response:
                response.raise_for_status()
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_BYTES:
                        raise ValueError("Response exceeds the 20 MB subtitle limit.")
                return bytes(data)

    def json(self, url: str, *, params=None, ttl: int = 900, headers=None):
        # Credentials are never stored in cache keys or response files.
        use_cache = self.cache and not headers and not (params or {}).get("api_key")
        key = hashlib.sha256((url + json.dumps(params, sort_keys=True)).encode()).hexdigest()
        path = self.cache_dir / f"{key}.json"
        if use_cache:
            try:
                if time.time() - path.stat().st_mtime < ttl:
                    return json.loads(path.read_text())
            except (OSError, ValueError):
                pass
        value = json.loads(
            self.fetch(url, params=params, headers={"Accept": "application/json", **(headers or {})})
        )
        if use_cache:
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(f".{os.getpid()}.tmp")
                tmp.write_text(json.dumps(value))
                tmp.replace(path)
            except OSError:
                pass
        return value


def error_message(exc: Exception) -> str:
    # HTTP exceptions can contain API keys in their request URLs.
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return {
            403: "access denied",
            429: "rate limited; try again later",
            401: "authentication required",
        }.get(code, f"HTTP {code}")
    if isinstance(exc, httpx.TimeoutException):
        return "timed out"
    if isinstance(exc, httpx.RequestError):
        return "connection failed"
    if isinstance(exc, (KeyError, TypeError, json.JSONDecodeError)):
        return "unexpected response from source"
    return str(exc)
