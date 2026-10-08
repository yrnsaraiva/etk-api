"""Enfileira o aviso ao parceiro quando o pagamento é confirmado ou
reembolsado.

Sem isto, o site do parceiro guarda `payment: pending` no momento da criação
e nunca mais sabe que o bilhete foi pago — foi exatamente o que aconteceu no
runwithbroto, onde o scanner compara com um valor local desatualizado.

`notify_partner()` só cria a linha em `PartnerDelivery`, dentro da mesma
transação que muda o estado do bilhete — nunca faz o pedido HTTP aqui. Um
parceiro lento não pode prender um worker do webhook da Debito Pay à espera
da resposta, e um aviso nunca se perde só porque o parceiro estava em baixo
no momento exato da confirmação. A entrega de facto corre à parte, no
comando `deliver_webhooks` (cron, a cada minuto).
"""

import hashlib
import hmac
import json
import logging
from datetime import timedelta

import requests
from django.utils import timezone

from .models import PartnerDelivery

logger = logging.getLogger(__name__)

# Espera crescente entre tentativas (minutos); mantém-se em 60 depois da
# última. Desiste de vez ao fim de GIVE_UP_AFTER, sem voltar a tentar.
BACKOFF_MINUTES = [1, 5, 15, 60]
GIVE_UP_AFTER = timedelta(days=1)


def sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def notify_partner(ticket, event_name: str = PartnerDelivery.Event.TICKET_PAID) -> None:
    owner = ticket.issued_to

    targets = owner.webhook_endpoints_to_notify()
    if not targets:
        logger.info("Ticket %s: parceiro não possui webhook configurado.", ticket.id)
        return

    # Uma entrega por destino: cada um tem o seu id, o seu segredo e a sua espera entre tentativas
    for endpoint in targets:
        PartnerDelivery.objects.create(
            ticket=ticket, event=event_name, payload=ticket.to_api(), endpoint=endpoint,
        )


def deliver_pending_webhooks() -> dict:
    """Envia as notificações enfileiradas por `notify_partner`. Correr a
    cada minuto (cron/`deliver_webhooks`).

    `X-ETK-Delivery-ID` leva o id da própria linha, não o id do bilhete —
    um bilhete pode gerar mais do que uma entrega (pago, depois reembolsado),
    e cada uma precisa de um id próprio para o parceiro filtrar repetidos.
    """
    now = timezone.now()
    stats = {"entregues": 0, "falharam": 0, "desistidos": 0}

    pendentes = PartnerDelivery.objects.filter(
        delivered_at__isnull=True, gave_up_at__isnull=True, next_attempt_at__lte=now,
    ).select_related("ticket__issued_to", "endpoint")

    for entrega in pendentes:
        if now - entrega.created_at > GIVE_UP_AFTER:
            entrega.gave_up_at = now
            entrega.save(update_fields=["gave_up_at"])
            logger.error(
                "desistiu de entregar o aviso #%s (%s) ao bilhete %s após %s",
                entrega.pk, entrega.event, entrega.ticket_id, GIVE_UP_AFTER,
            )
            stats["desistidos"] += 1
            continue

        if _deliver_one(entrega, now):
            stats["entregues"] += 1
        else:
            stats["falharam"] += 1
    return stats


def _target(entrega: PartnerDelivery):
    """(url, segredo) do destino, ou None se já não existe/está desligado (o aviso foi enfileirado antes)."""
    if entrega.endpoint_id:
        ep = entrega.endpoint
        return (ep.url, ep.secret) if ep.is_active and ep.url else None
    owner = entrega.ticket.issued_to
    return (owner.webhook_url, owner.webhook_secret) if owner.webhook_url else None


def _deliver_one(entrega: PartnerDelivery, now) -> bool:
    target = _target(entrega)
    if target is None:
        entrega.gave_up_at = now
        entrega.last_error = "destino removido ou desligado"
        entrega.save(update_fields=["gave_up_at", "last_error"])
        logger.warning("aviso #%s: destino já não está activo, descartado", entrega.pk)
        return False
    url, secret = target
    body = json.dumps(
        {"event": entrega.event, "data": entrega.payload}, separators=(",", ":")
    ).encode()
    headers = {
        "Content-Type": "application/json",
        "X-ETK-Signature": sign(body, secret or ""),
        "X-ETK-Event": entrega.event,
        "X-ETK-Delivery-ID": str(entrega.pk),
    }

    erro = ""
    try:
        response = requests.post(url, data=body, headers=headers, timeout=(3, 30))
        ok = 200 <= response.status_code < 300
        if not ok:
            erro = f"HTTP {response.status_code}: {response.text[:200]}"
    except requests.RequestException as exc:
        ok = False
        erro = str(exc)[:255]

    entrega.attempts += 1
    if ok:
        entrega.delivered_at = now
        entrega.last_error = ""
        entrega.save(update_fields=["attempts", "delivered_at", "last_error"])
        logger.info("aviso #%s entregue ao bilhete %s", entrega.pk, entrega.ticket_id)
        return True

    delay = BACKOFF_MINUTES[min(entrega.attempts - 1, len(BACKOFF_MINUTES) - 1)]
    entrega.next_attempt_at = now + timedelta(minutes=delay)
    entrega.last_error = erro
    entrega.save(update_fields=["attempts", "next_attempt_at", "last_error"])
    logger.warning(
        "aviso #%s ao bilhete %s falhou (tentativa %s): %s",
        entrega.pk, entrega.ticket_id, entrega.attempts, erro,
    )
    return False
