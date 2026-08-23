"""Notificação ao parceiro quando o pagamento é confirmado.

Sem isto, o site do parceiro guarda `payment: pending` no momento da criação e
nunca mais sabe que o bilhete foi pago — foi exatamente o que aconteceu no
runwithbroto, onde o scanner compara com um valor local desatualizado.
"""

import hashlib
import hmac
import json
import logging

import requests

logger = logging.getLogger(__name__)


def sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def notify_partner(ticket, event_name: str = "ticket.paid") -> bool:
    owner = ticket.issued_to
    if not owner.webhook_url:
        return False
    body = json.dumps({"event": event_name, "data": ticket.to_api()}).encode()
    try:
        resp = requests.post(
            owner.webhook_url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-ETK-Signature": sign(body, owner.webhook_secret or ""),
            },
            timeout=10,
        )
        return resp.ok
    except requests.RequestException as exc:
        logger.warning("webhook falhou para %s: %s", owner.webhook_url, exc)
        return False
