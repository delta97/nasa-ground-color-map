"""Webhook validation, signing, and bounded retries."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import socket
from urllib.parse import urlparse


def validate_webhook_url(url: str, allow_private: bool = False) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("webhook URL must be HTTPS and must not contain credentials")
    if allow_private: return url
    try: addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)}
    except socket.gaierror as exc: raise ValueError("webhook hostname could not be resolved") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError("webhook destination must not be private, loopback, link-local, or reserved")
    return url


def encode_and_sign(payload: dict, secret: str) -> tuple[bytes, str]:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return body, "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def deliver(http, url: str, payload: dict, secret: str, record_attempt):
    body, signature = encode_and_sign(payload, secret)
    for attempt in range(1, 4):
        try:
            response = await http.post(url, content=body, headers={"Content-Type": "application/json", "X-Ground-Truth-Signature": signature})
            await record_attempt(attempt, response.status_code, None)
            if 200 <= response.status_code < 300: return True
        except Exception as exc:
            await record_attempt(attempt, None, str(exc))
        if attempt < 3: await asyncio.sleep(2 ** (attempt - 1))
    return False
