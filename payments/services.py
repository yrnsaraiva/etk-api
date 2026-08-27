"""Orquestração do pagamento. Duas regras que evitam fraude e dinheiro perdido:

1. Nunca confiar no valor que vem do gateway sem comparar com o bilhete —
   isto vale tanto para o webhook como para a confirmação síncrona.
2. Nunca depender só do webhook — os webhooks perdem-se, e em mobile money
   perdem-se com frequência. Daí a reconciliação.

A Debito Pay tem uma particularidade que não é o padrão de mercado: M-Pesa
confirma de forma SÍNCRONA, na própria resposta ao pedido de cobrança — não
espera por webhook. `start_payment` trata esse caso; e-Mola, mKesh e cartões
continuam a depender do webhook ou da reconciliação.
"""

import logging
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from ticketing.models import PaymentAttempt, Ticket
from ticketing.services import confirm_payment, release
from ticketing.webhooks import notify_partner

from .models import ProviderEvent
from .providers.base import FAILED, PENDING, SUCCEEDED, Charge, PaymentError
from .providers.registry import get_provider

logger = logging.getLogger(__name__)


def start_payment(ticket: Ticket, *, callback_url: str, provider_name: str | None = None) -> Charge:
    """Cria a cobrança no gateway e guarda a referência no bilhete.

    Se o gateway confirmar de imediato (M-Pesa síncrono), o bilhete já sai
    daqui como `paid` — não fica à espera de um webhook que não vai chegar.
    """
    provider = get_provider(provider_name)
    charge = provider.create_charge(
        amount=ticket.amount,
        currency=ticket.currency,
        reference=ticket.id,                     # o nosso id é a chave de idempotência
        phone=ticket.phone,
        method=ticket.payment_method,
        description=f"{ticket.event.name} — {ticket.price.name}",
        callback_url=callback_url,
    )
    Ticket.objects.filter(pk=ticket.pk).update(
        provider=provider.name,
        provider_charge_id=charge.reference,
        checkout_url=charge.checkout_url,
        updated_at=timezone.now(),
    )
    PaymentAttempt.objects.create(
        ticket=ticket, provider=provider.name, provider_reference=charge.reference,
        amount=ticket.amount, succeeded=(charge.status == SUCCEEDED), raw_payload=charge.raw,
    )

    if charge.status == SUCCEEDED:
        ticket.refresh_from_db()
        _settle(ticket, status=SUCCEEDED, amount=None, currency=None,
               provider_name=provider.name, reference=charge.reference, raw=charge.raw)
    elif charge.status == FAILED:
        ticket.refresh_from_db()
        release(ticket, Ticket.Payment.FAILED)

    return charge


def handle_webhook(body: bytes, headers, provider_name: str | None = None) -> tuple[bool, str]:
    """Devolve (processado, mensagem). Levanta InvalidSignature se não for autêntico."""
    provider = get_provider(provider_name)
    event = provider.parse_webhook(body, headers)   # valida assinatura

    try:
        with transaction.atomic():
            record = ProviderEvent.objects.create(
                provider=provider.name, event_id=event.event_id,
                event_type=event.type, charge_reference=event.charge_reference,
                payload=event.raw,
            )
    except IntegrityError:
        return False, "Evento já recebido (ignorado)."   # reenvio do gateway

    try:
        ticket = Ticket.objects.select_related("price__event", "issued_to").get(
            provider_charge_id=event.charge_reference
        )
    except Ticket.DoesNotExist:
        logger.warning("webhook para cobrança desconhecida: %s", event.charge_reference)
        outcome = "Cobrança desconhecida."
    else:
        outcome = _settle(ticket, status=event.status, amount=event.amount,
                          currency=event.currency, provider_name=provider.name,
                          reference=event.charge_reference, raw=event.raw)

    ProviderEvent.objects.filter(pk=record.pk).update(
        processed_at=timezone.now(), outcome=outcome[:200]
    )
    return True, outcome


def _settle(ticket: Ticket, *, status: str, amount, currency: str | None,
           provider_name: str, reference: str, raw: dict) -> str:
    """Aplica um resultado de pagamento a um bilhete. Ponto único usado pela
    confirmação síncrona, pelo webhook e pela reconciliação — as três formas
    de saber que um pagamento aconteceu passam sempre pela mesma verificação.
    """
    if status == SUCCEEDED:
        if amount is not None and Decimal(amount) != ticket.amount:
            logger.error(
                "valor divergente no bilhete %s: cobrado %s, esperado %s",
                ticket.id, amount, ticket.amount,
            )
            return "Valor divergente — retido para revisão manual."
        if currency and currency != ticket.currency:
            logger.error(
                "moeda divergente no bilhete %s: recebida %s, esperada %s",
                ticket.id, currency, ticket.currency,
            )
            return "Moeda divergente — retido para revisão manual."

        confirm_payment(ticket, provider=provider_name, provider_reference=reference, payload=raw)
        notify_partner(ticket)
        return "Pagamento confirmado."

    if status == FAILED:
        release(ticket, Ticket.Payment.FAILED)
        return "Pagamento falhou — vaga libertada."

    return "Estado pendente — sem alteração."


def reconcile_pending(limit: int = 200) -> dict:
    """Sonda o gateway sobre bilhetes ainda pendentes.

    Correr a cada poucos minutos. É isto que salva o cliente que pagou por
    e-Mola/mKesh/cartão e cujo webhook nunca chegou — sem esta rotina, fica
    à porta com o dinheiro fora. (M-Pesa raramente chega aqui pendente,
    porque confirma de forma síncrona em start_payment.)
    """
    provider = get_provider()
    pending = Ticket.objects.filter(
        payment=Ticket.Payment.PENDING, provider_charge_id__gt=""
    ).select_related("price__event", "issued_to")[:limit]

    stats = {"verificados": 0, "confirmados": 0, "falhados": 0, "erros": 0}
    for ticket in pending:
        stats["verificados"] += 1
        try:
            charge = provider.fetch_charge(ticket.provider_charge_id)
        except PaymentError as exc:
            logger.warning("reconciliação falhou para %s: %s", ticket.id, exc)
            stats["erros"] += 1
            continue

        if charge.status == SUCCEEDED:
            outcome = _settle(ticket, status=SUCCEEDED, amount=charge.amount,
                              currency=charge.currency, provider_name=provider.name,
                              reference=charge.reference, raw=charge.raw)
            if outcome == "Pagamento confirmado.":
                stats["confirmados"] += 1
            else:
                stats["erros"] += 1     # valor/moeda divergente — fica para revisão
        elif charge.status == FAILED:
            release(ticket, Ticket.Payment.FAILED)
            stats["falhados"] += 1
        elif ticket.expires_at and ticket.expires_at < timezone.now():
            # Continua PENDING no gateway mas a reserva expirou: liberta a vaga,
            # sem marcar como falhado — o dinheiro pode ainda chegar.
            release(ticket, Ticket.Payment.FAILED)
            stats["falhados"] += 1
    return stats
