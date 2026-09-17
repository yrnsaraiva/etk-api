"""Orquestração do pagamento. Duas regras que evitam fraude e dinheiro perdido:

1. Nunca confiar no valor que vem do gateway sem comparar com o bilhete —
   vale tanto para o webhook como para a confirmação síncrona.
2. Nunca depender só do webhook — perdem-se, e em mobile money com frequência.
   Daí a reconciliação (reconcile_pending).

Regra nova (a corrigir o bug dos tickets pending duplicados):
3. Qualquer falha ao criar a cobrança — recusa da Debito Pay, timeout, ou um
   erro inesperado — LIBERTA a vaga já. Nunca fica um ticket pending sem
   cobrança associada à espera do cron de expiração (15 min). Se o parceiro
   repetir o pedido com o mesmo `external_reference`, o ticket antigo já
   está `failed` e a vaga livre, por isso cria-se um ticket novo em vez de
   reservar em cima do anterior.
"""

import logging
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from ticketing.models import PaymentAttempt, Ticket
from ticketing.services import confirm_payment, release
from ticketing.webhooks import notify_partner

from . import debitopay
from .debitopay import FAILED, PENDING, SUCCEEDED, PROVIDER_NAME, Charge
from .exceptions import PaymentError
from .models import ProviderEvent

logger = logging.getLogger(__name__)


def start_payment(ticket: Ticket, *, callback_url: str) -> Charge:
    """Cria a cobrança na Debito Pay e guarda a referência no bilhete.

    Se a Debito Pay confirmar de imediato (M-Pesa síncrono), o bilhete já sai
    daqui como `paid`. Se a cobrança falhar por qualquer motivo, o bilhete
    sai daqui como `failed` e a vaga já foi devolvida — nunca fica pending
    sem cobrança associada.
    """
    try:
        charge = debitopay.create_charge(
            amount=ticket.amount,
            currency=ticket.currency,
            reference=ticket.id,                     # o nosso id é a chave de idempotência
            phone=ticket.phone,
            method=ticket.payment_method,
            description=f"{ticket.event.name} — {ticket.price.name}",
            callback_url=callback_url,
        )
    except PaymentError as exc:
        # Cobre tanto recusa (PaymentDeclined) como falha de transporte
        # (ProviderUnavailable) e qualquer outro erro de configuração. Em
        # todos os casos: não há cobrança criada, por isso não faz sentido
        # deixar o ticket pending — regista a tentativa falhada e liberta.
        PaymentAttempt.objects.create(
            ticket=ticket, provider=PROVIDER_NAME, provider_reference="",
            amount=ticket.amount, succeeded=False,
            raw_payload={"error": str(exc), "code": getattr(exc, "code", None)},
        )
        release(ticket, Ticket.Payment.FAILED)
        raise

    Ticket.objects.filter(pk=ticket.pk).update(
        provider=PROVIDER_NAME,
        provider_charge_id=charge.reference,
        checkout_url=charge.checkout_url,
        updated_at=timezone.now(),
    )
    PaymentAttempt.objects.create(
        ticket=ticket, provider=PROVIDER_NAME, provider_reference=charge.reference,
        amount=ticket.amount, succeeded=(charge.status == SUCCEEDED), raw_payload=charge.raw,
    )

    if charge.status == SUCCEEDED:
        ticket.refresh_from_db()
        _settle(ticket, status=SUCCEEDED, amount=None, currency=None,
               reference=charge.reference, raw=charge.raw)
    elif charge.status == FAILED:
        ticket.refresh_from_db()
        release(ticket, Ticket.Payment.FAILED)

    return charge


def handle_webhook(body: bytes, headers) -> tuple[bool, str]:
    """Devolve (processado, mensagem). Levanta InvalidSignature se não for autêntico."""
    event = debitopay.parse_webhook(body, headers)   # valida assinatura

    try:
        with transaction.atomic():
            record = ProviderEvent.objects.create(
                provider=PROVIDER_NAME, event_id=event.event_id,
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
                          currency=event.currency, reference=event.charge_reference, raw=event.raw)

    ProviderEvent.objects.filter(pk=record.pk).update(
        processed_at=timezone.now(), outcome=outcome[:200]
    )
    return True, outcome


def _settle(ticket: Ticket, *, status: str, amount, currency: str | None,
           reference: str, raw: dict) -> str:
    """Aplica um resultado de pagamento a um bilhete. Ponto único usado pela
    confirmação síncrona, pelo webhook e pela reconciliação.
    """
    if status == SUCCEEDED:
        if amount is not None and Decimal(amount) != ticket.amount:
            logger.error("valor divergente no bilhete %s: cobrado %s, esperado %s",
                        ticket.id, amount, ticket.amount)
            return "Valor divergente — retido para revisão manual."
        if currency and currency != ticket.currency:
            logger.error("moeda divergente no bilhete %s: recebida %s, esperada %s",
                        ticket.id, currency, ticket.currency)
            return "Moeda divergente — retido para revisão manual."

        confirm_payment(ticket, provider=PROVIDER_NAME, provider_reference=reference, payload=raw)
        notify_partner(ticket)
        return "Pagamento confirmado."

    if status == FAILED:
        release(ticket, Ticket.Payment.FAILED)
        return "Pagamento falhou — vaga libertada."

    return "Estado pendente — sem alteração."


def reconcile_pending(limit: int = 200) -> dict:
    """Sonda a Debito Pay sobre bilhetes ainda pendentes.

    Correr a cada poucos minutos. Salva o cliente que pagou por e-Mola/mKesh/
    cartão e cujo webhook nunca chegou. (M-Pesa raramente chega aqui pendente,
    porque confirma de forma síncrona em start_payment.)
    """
    pending = Ticket.objects.filter(
        payment=Ticket.Payment.PENDING, provider_charge_id__gt=""
    ).select_related("price__event", "issued_to")[:limit]

    stats = {"verificados": 0, "confirmados": 0, "falhados": 0, "erros": 0}
    for ticket in pending:
        stats["verificados"] += 1
        try:
            charge = debitopay.fetch_charge(ticket.provider_charge_id)
        except PaymentError as exc:
            logger.warning("reconciliação falhou para %s: %s", ticket.id, exc)
            stats["erros"] += 1
            continue

        if charge.status == SUCCEEDED:
            outcome = _settle(ticket, status=SUCCEEDED, amount=charge.amount,
                              currency=charge.currency, reference=charge.reference, raw=charge.raw)
            if outcome == "Pagamento confirmado.":
                stats["confirmados"] += 1
            else:
                stats["erros"] += 1     # valor/moeda divergente — fica para revisão
        elif charge.status == FAILED:
            release(ticket, Ticket.Payment.FAILED)
            stats["falhados"] += 1
        elif ticket.expires_at and ticket.expires_at < timezone.now():
            # Continua PENDING no gateway mas a reserva expirou: liberta a
            # vaga, sem marcar como falhado — o dinheiro pode ainda chegar.
            release(ticket, Ticket.Payment.FAILED)
            stats["falhados"] += 1
    return stats