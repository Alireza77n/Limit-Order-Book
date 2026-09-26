"""Nobitex API-Key authentication helpers.

Nobitex API-key signatures cover the exact request representation::

    timestamp + METHOD + full_path_with_query_string + raw_body

For that reason callers should build query strings and JSON bodies with the
helpers in this module and send exactly the returned strings.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Mapping, Sequence
from typing import Any, Optional, Union
from urllib.parse import urlencode

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


QueryParams = Optional[
    Union[
        Mapping[str, Any],
        Sequence[tuple[str, Any]],
    ]
]


def compact_json(data: Any) -> str:
    """Serialize JSON exactly once for both signing and HTTP transmission."""
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


def build_full_path(path: str, params: QueryParams = None) -> str:
    """Return ``path`` plus an encoded query string, preserving item order.

    The resulting value is suitable both for the URL and for Nobitex's
    ``full_path`` signature component. ``None`` query values are omitted, as
    ``requests`` normally does for ``params={...}``.
    """
    if not path.startswith("/"):
        raise ValueError("Nobitex API path must start with '/'")
    if not params:
        return path

    items = list(params.items()) if isinstance(params, Mapping) else list(params)
    filtered = [(key, value) for key, value in items if value is not None]
    if not filtered:
        return path

    query = urlencode(filtered, doseq=True)
    return f"{path}?{query}" if query else path


def _decode_private_key(secret_key: str) -> bytes:
    if not secret_key:
        raise ValueError("Nobitex secret key is required")
    try:
        padded = secret_key + "=" * (-len(secret_key) % 4)
        private_bytes = base64.urlsafe_b64decode(padded.encode("ascii"))
    except Exception as exc:  # binascii.Error / UnicodeEncodeError
        raise ValueError("Nobitex secret key is not valid URL-safe Base64") from exc

    if len(private_bytes) != 32:
        raise ValueError(
            f"Nobitex Ed25519 secret key must decode to 32 bytes, got {len(private_bytes)}"
        )
    return private_bytes


def create_signature(
    secret_key: str,
    method: str,
    full_path: str,
    body: str = "",
    *,
    timestamp: Optional[str] = None,
):
    """Create an Ed25519 Nobitex API-key signature.

    ``full_path`` must include the query string for requests that have one.
    ``body`` must be the exact raw body transmitted on the wire.
    """
    if not full_path.startswith("/"):
        raise ValueError("Nobitex full_path must start with '/'")
    timestamp = str(int(time.time())) if timestamp is None else str(timestamp)
    method = method.upper()
    message = (timestamp + method + full_path + body).encode("utf-8")
    private_key = Ed25519PrivateKey.from_private_bytes(_decode_private_key(secret_key))
    signature = private_key.sign(message)
    signature_b64 = base64.urlsafe_b64encode(signature).decode("ascii")
    return timestamp, signature_b64


def signed_headers(
    api_key: str,
    secret_key: str,
    method: str,
    full_path: str,
    body: str = "",
    *,
    timestamp: Optional[str] = None,
):
    """Return fresh Nobitex API-key headers for one exact HTTP request."""
    if not api_key:
        raise ValueError("Nobitex API key is required")
    ts, signature = create_signature(
        secret_key, method, full_path, body, timestamp=timestamp
    )
    return {
        "Nobitex-Key": api_key,
        "Nobitex-Signature": signature,
        "Nobitex-Timestamp": ts,
        "Content-Type": "application/json",
    }
