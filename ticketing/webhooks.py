"""Notificação ao parceiro quando o pagamento é confirmado.

Sem isto, o site do parceiro guarda `payment: pending` no momento da criação e
nunca mais sabe que o bilhete foi pago — foi exatamente o que aconteceu no
runwithbroto, onde o scanner compara com um valor local desatualizado.
"""

import hashlib
import hmac
import json
import logging
import time

import requests

logger = logging.getLogger(__name__)


def sign(body: bytes, secret: str) -> str:
    return hmac.new(
        secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()


def notify_partner(
    ticket,
    event_name: str = "ticket.paid",
    max_retries: int = 3,
) -> bool:
    owner = ticket.issued_to

    if not owner.webhook_url:
        logger.info(
            "Ticket %s: parceiro não possui webhook configurado.",
            ticket.id,
        )
        return False

    body = json.dumps(
        {
            "event": event_name,
            "data": ticket.to_api(),
        },
        separators=(",", ":"),
    ).encode()

    signature = sign(
        body,
        owner.webhook_secret or "",
    )

    headers = {
        "Content-Type": "application/json",
        "X-ETK-Signature": signature,
        "X-ETK-Event": event_name,
        "X-ETK-Delivery-ID": str(ticket.id),
    }

    for attempt in range(1, max_retries + 1):
        try:
            logger.info(
                "Webhook %s para %s — tentativa %s/%s",
                ticket.id,
                owner.webhook_url,
                attempt,
                max_retries,
            )

            response = requests.post(
                owner.webhook_url,
                data=body,
                headers=headers,
                timeout=(3, 10),
            )

            if 200 <= response.status_code < 300:
                logger.info(
                    "Webhook entregue com sucesso. "
                    "Ticket=%s status=%s",
                    ticket.id,
                    response.status_code,
                )
                return True

            logger.warning(
                "Webhook rejeitado. "
                "Ticket=%s status=%s resposta=%s",
                ticket.id,
                response.status_code,
                response.text[:500],
            )

            # Erros 4xx normalmente não serão resolvidos
            # repetindo imediatamente.
            if 400 <= response.status_code < 500:
                return False

        except requests.Timeout:
            logger.warning(
                "Timeout no webhook. "
                "Ticket=%s tentativa=%s/%s",
                ticket.id,
                attempt,
                max_retries,
            )

        except requests.RequestException as exc:
            logger.warning(
                "Erro no webhook. "
                "Ticket=%s tentativa=%s/%s erro=%s",
                ticket.id,
                attempt,
                max_retries,
                exc,
            )

        if attempt < max_retries:
            # 2s, 4s
            delay = 2 ** attempt

            logger.info(
                "Nova tentativa do webhook em %s segundos.",
                delay,
            )

            time.sleep(delay)

    logger.error(
        "Webhook falhou definitivamente após %s tentativas. "
        "Ticket=%s",
        max_retries,
        ticket.id,
    )

    return False
