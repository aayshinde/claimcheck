"""Outbound webhooks: tell other systems (the firm's CMS, Slack, etc.) when something needs attention.

Each delivery is signed with HMAC-SHA256 (header X-ClaimCheck-Signature) so the receiver can reject forged calls, and
every attempt is recorded in webhook_deliveries. Delivery failures never break the pipeline.
"""
from __future__ import annotations

import hashlib
import hmac
import json

import httpx

from claimcheck import config
from claimcheck.db import WebhookDelivery, session_scope


def sign(body: bytes, secret: str = config.WEBHOOK_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def notify(event: str, payload: dict, url: str | None = None, http=None) -> bool:
    url = url or config.WEBHOOK_URL
    if not url:
        return False
    body = json.dumps({"event": event, "data": payload}, default=str).encode()
    code, ok = None, False
    try:
        r = (http or httpx).post(url, content=body, timeout=3.0,
                                 headers={"Content-Type": "application/json", "X-ClaimCheck-Event": event,
                                          "X-ClaimCheck-Signature": sign(body)})
        code, ok = r.status_code, 200 <= r.status_code < 300
    except Exception:
        pass
    with session_scope() as s:
        s.add(WebhookDelivery(event=event, url=url, payload=payload, status_code=code, ok=ok))
    return ok
