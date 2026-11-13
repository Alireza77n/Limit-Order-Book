"""Mazdax API-key authentication (https://api-docs.mazdax.tech/authentication).

Every private request carries all of its parameters in the query string, a millisecond ``timestamp``
among them, and is signed with HMAC-SHA256::

    sign_payload = METHOD + "\\n" + PATH + "\\n" + query_string
    signature    = hex(HMAC_SHA256(secret, sign_payload))

``PATH`` has no host and no query; ``query_string`` is exactly the string sent in the URL, in the same
order, without ``signature``, which is appended last. The key itself goes in the ``X-MZX-APIKEY``
header and the body stays empty. Mazdax rejects a timestamp more than 5 seconds from its own clock.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Mapping, Sequence
from typing import Any, Optional, Union
from urllib.parse import urlencode

API_KEY_HEADER = "X-MZX-APIKEY"

QueryParams = Optional[Union[Mapping[str, Any], Sequence[tuple[str, Any]]]]


def build_query(params: QueryParams = None) -> str:
    """Encode ``params`` in the given order; ``None`` values are left out, booleans become true/false."""
    if not params:
        return ""
    items = list(params.items()) if isinstance(params, Mapping) else list(params)
    encoded = []
    for key, value in items:
        if value is None:
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        encoded.append((key, value))
    return urlencode(encoded)


def sign(secret: str, method: str, path: str, query_string: str) -> str:
    if not secret:
        raise ValueError("Mazdax secret is required")
    if not path.startswith("/"):
        raise ValueError("Mazdax API path must start with '/'")
    payload = f"{method.upper()}\n{path}\n{query_string}"
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def signed_request(
    api_key: str,
    secret: str,
    method: str,
    path: str,
    params: QueryParams = None,
    *,
    timestamp_ms: Optional[int] = None,
) -> tuple[str, dict[str, str]]:
    """Return ``(path_with_signed_query, headers)`` for one private request.

    ``timestamp`` is appended after the caller's parameters and ``signature`` after that, so the signed
    string and the transmitted string are the same bytes.
    """
    if not api_key:
        raise ValueError("Mazdax API key is required")
    stamp = int(time.time() * 1000) if timestamp_ms is None else int(timestamp_ms)
    query = build_query(params)
    query = f"{query}&timestamp={stamp}" if query else f"timestamp={stamp}"
    signature = sign(secret, method, path, query)
    return f"{path}?{query}&signature={signature}", {API_KEY_HEADER: api_key}
