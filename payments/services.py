"""Orquestração do pagamento. Duas regras que evitam fraude e dinheiro perdido:

1. Nunca confiar no valor que vem no webhook — comparar com o bilhete.
2. Nunca depender só do webhook — os webhooks perdem-se, e em mobile money
   perdem-se com frequência. Daí a reconciliação.
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
    """Cria a cobrança no gateway e guarda a referência no bilhete."""
    provider = get_provider(provider_name)
    # NOTA: assume ticket.issued_to.get_full_name() / .email — confirma que
    # estes atributos existem no teu modelo de utilizador antes de assumir
    # que isto está correcto; ajusta se os nomes forem outros.
    customer_name = ticket.issued_to.get_full_name() if ticket.issued_to else ""
    customer_email = getattr(ticket.issued_to, "email", "") if ticket.issued_to else ""
    charge = provider.create_charge(
        amount=ticket.amount,
        currency=ticket.currency,
        reference=ticket.id,                     # o nosso id é a chave de idempotência
        phone=ticket.phone,
        method=ticket.payment_method,
        description=f"{ticket.event.name} — {ticket.price.name}",
        callback_url=callback_url,
        customer_name=customer_name,
        customer_email=customer_email,
        customer_phone=ticket.phone,
    )
    Ticket.objects.filter(pk=ticket.pk).update(
        provider=provider.name,
        provider_charge_id=charge.reference,
        checkout_url=charge.checkout_url,
        updated_at=timezone.now(),
    )
    PaymentAttempt.objects.create(
        ticket=ticket, provider=provider.name, provider_reference=charge.reference,
        amount=ticket.amount, succeeded=False, raw_payload=charge.raw,
    )
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

    outcome = _apply(event)
    ProviderEvent.objects.filter(pk=record.pk).update(
        processed_at=timezone.now(), outcome=outcome[:200]
    )
    return True, outcome


def _apply(event) -> str:
    try:
        ticket = Ticket.objects.select_related("price__event", "issued_to").get(
            provider_charge_id=event.charge_reference
        )
    except Ticket.DoesNotExist:
        logger.warning("webhook para cobrança desconhecida: %s", event.charge_reference)
        return "Cobrança desconhecida."

    if event.status == SUCCEEDED:
        # --- verificação anti-fraude: o valor pago tem de bater com o devido ---
        if event.amount is not None and Decimal(event.amount) != ticket.amount:
            logger.error(
                "valor divergente no bilhete %s: cobrado %s, esperado %s",
                ticket.id, event.amount, ticket.amount,
            )
            return "Valor divergente — retido para revisão manual."
        if event.currency and event.currency != ticket.currency:
            return "Moeda divergente — retido para revisão manual."

        confirm_payment(
            ticket, provider=event.raw.get("provider", "debitopay"),
            provider_reference=event.charge_reference, payload=event.raw,
        )
        notify_partner(ticket)
        return "Pagamento confirmado."

    if event.status == FAILED:
        release(ticket, Ticket.Payment.FAILED)
        return "Pagamento falhou — vaga libertada."

    return "Estado pendente — sem alteração."


def reconcile_pending(limit: int = 200) -> dict:
    """Sonda o gateway sobre bilhetes ainda pendentes.

    Correr a cada poucos minutos. É isto que salva o cliente que pagou e cujo
    webhook nunca chegou — sem esta rotina, fica à porta com o dinheiro fora.
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
            if charge.amount != ticket.amount:
                logger.error("valor divergente na reconciliação de %s", ticket.id)
                stats["erros"] += 1
                continue
            confirm_payment(
                ticket, provider=provider.name,
                provider_reference=charge.reference, payload=charge.raw,
            )
            notify_partner(ticket)
            stats["confirmados"] += 1
        elif charge.status == FAILED:
            release(ticket, Ticket.Payment.FAILED)
            stats["falhados"] += 1
        elif ticket.expires_at and ticket.expires_at < timezone.now():
            # Continua PENDING no gateway mas a reserva expirou: liberta a vaga,
            # sem marcar como falhado — o dinheiro pode ainda chegar.
            release(ticket, Ticket.Payment.FAILED)
            stats["falhados"] += 1
    return stats