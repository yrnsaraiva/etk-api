"""Regras de negócio. O ponto crítico é não vender mais bilhetes do que existem."""

import hashlib
import hmac
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from catalog.models import Event, Price

from .models import CheckInLog, PaymentAttempt, Ticket


class TicketError(ValidationError):
    pass


@transaction.atomic
def create_ticket(*, price_id: str, event_id: str, phone: str, issued_to,
                  full_name: str = "", email: str = "", payment_method: str = "") -> Ticket:
    """Emite um bilhete `pending` e reserva o lugar.

    `select_for_update()` tranca a linha do Price até ao fim da transação. Sem
    isto, dois pedidos simultâneos leem "resta 1", ambos passam na verificação
    e vendem-se dois bilhetes para uma vaga.
    """
    try:
        price = (
            Price.objects.select_for_update().select_related("event").get(pk=price_id)
        )
    except Price.DoesNotExist:
        raise TicketError("priceId inválido.")

    if price.event_id != event_id:
        raise TicketError("O priceId não pertence a este eventId.")
    if price.event.organizer_id != issued_to.pk:
        # Não revela que o evento existe noutro organizador — a mesma
        # mensagem de "não pertence" cobre os dois casos.
        raise TicketError("O priceId não pertence a este eventId.")
    if price.event.status != Event.Status.PUBLISHED:
        raise TicketError("Este evento não está disponível.")
    if not price.is_on_sale():
        raise TicketError(
            "Bilhetes esgotados." if price.available == 0 else "Este preço não está à venda."
        )

    Price.objects.filter(pk=price.pk).update(quantity_reserved=F("quantity_reserved") + 1)
    if price.available - 1 <= 0:
        Price.objects.filter(pk=price.pk).update(status=Price.Status.SOLD_OUT)

    return Ticket.objects.create(
        price=price,
        amount=price.amount,          # congelado aqui
        currency=price.currency,
        issued_to=issued_to,
        phone=phone,
        full_name=full_name,
        email=email,
        payment_method=payment_method,
        expires_at=timezone.now() + timedelta(minutes=settings.TICKET_RESERVATION_MINUTES),
    )


@transaction.atomic
def confirm_payment(ticket: Ticket, *, provider: str, provider_reference: str,
                    payload: dict | None = None) -> Ticket:
    """Confirma o pagamento. Idempotente: callback repetido não duplica nada."""
    ticket = Ticket.objects.select_for_update().select_related("price").get(pk=ticket.pk)

    if ticket.payment == Ticket.Payment.PAID:
        return ticket
    if ticket.payment != Ticket.Payment.PENDING:
        raise TicketError(f"Bilhete em estado '{ticket.payment}'.")
    if ticket.expires_at and ticket.expires_at < timezone.now():
        release(ticket, Ticket.Payment.FAILED)
        raise TicketError("A reserva expirou.")

    PaymentAttempt.objects.create(
        ticket=ticket, provider=provider, provider_reference=provider_reference,
        amount=ticket.amount, succeeded=True, raw_payload=payload or {},
    )
    ticket.payment = Ticket.Payment.PAID
    ticket.paid_at = timezone.now()
    ticket.expires_at = None
    ticket.save(update_fields=["payment", "paid_at", "expires_at", "updated_at"])
    return ticket


@transaction.atomic
def release(ticket: Ticket, payment_status: str) -> Ticket:
    """Devolve a vaga ao lote."""
    ticket = Ticket.objects.select_for_update().get(pk=ticket.pk)
    if ticket.payment != Ticket.Payment.PENDING:
        return ticket
    Price.objects.filter(pk=ticket.price_id).update(
        quantity_reserved=F("quantity_reserved") - 1
    )
    Price.objects.filter(pk=ticket.price_id, status=Price.Status.SOLD_OUT).update(
        status=Price.Status.ACTIVE
    )
    ticket.payment = payment_status
    ticket.status = Ticket.Status.EXPIRED
    ticket.save(update_fields=["payment", "status", "updated_at"])
    return ticket


def expire_stale_tickets() -> int:
    """Correr a cada minuto (cron / Celery beat) para libertar vagas não pagas."""
    stale = Ticket.objects.filter(
        payment=Ticket.Payment.PENDING, expires_at__lt=timezone.now()
    )
    count = 0
    for ticket in stale:
        release(ticket, Ticket.Payment.FAILED)
        count += 1
    return count


def parse_qr(qr_value: str) -> str | None:
    """Aceita `TCKT…` ou `TCKT…|assinatura`, verificando o HMAC quando presente."""
    qr_value = (qr_value or "").strip()
    if not qr_value.startswith("TCKT"):
        return None
    if "|" not in qr_value:
        return qr_value
    ticket_id, _, sig = qr_value.partition("|")
    expected = hmac.new(
        settings.SECRET_KEY.encode(), ticket_id.encode(), hashlib.sha256
    ).hexdigest()[:16]
    return ticket_id if hmac.compare_digest(expected, sig) else None


@transaction.atomic
def check_in(*, qr_value: str, staff_user) -> tuple[str, str, Ticket | None]:
    """Devolve (resultado, mensagem, bilhete). Não levanta exceção: o porteiro
    precisa sempre de uma resposta legível, mesmo para um QR de outra feira."""

    def log(result, ticket_id=""):
        CheckInLog.objects.create(
            ticket_id_raw=ticket_id, result=result, scanned_by=staff_user, raw_qr=qr_value[:255]
        )

    ticket_id = parse_qr(qr_value)
    if not ticket_id:
        log(CheckInLog.Result.INVALID_QR)
        return CheckInLog.Result.INVALID_QR, "QR não reconhecido.", None

    try:
        ticket = (
            Ticket.objects.select_for_update()
            .select_related("price__event")
            .get(pk=ticket_id)
        )
    except Ticket.DoesNotExist:
        log(CheckInLog.Result.NOT_FOUND, ticket_id)
        return CheckInLog.Result.NOT_FOUND, f"Bilhete '{ticket_id}' não encontrado.", None

    if ticket.event.organizer_id != staff_user.pk:
        log(CheckInLog.Result.NOT_FOUND, ticket_id)
        return CheckInLog.Result.NOT_FOUND, "Bilhete de outro evento.", None

    if ticket.payment != Ticket.Payment.PAID:
        log(CheckInLog.Result.NOT_PAID, ticket_id)
        return CheckInLog.Result.NOT_PAID, f"Pagamento não confirmado ({ticket.payment}).", ticket

    if ticket.entered:
        log(CheckInLog.Result.ALREADY_ENTERED, ticket_id)
        when = timezone.localtime(ticket.entered_at).strftime("%H:%M")
        return CheckInLog.Result.ALREADY_ENTERED, f"Já entrou às {when}.", ticket

    ticket.entered = True
    ticket.entered_at = timezone.now()
    ticket.save(update_fields=["entered", "entered_at", "updated_at"])
    log(CheckInLog.Result.OK, ticket_id)
    return CheckInLog.Result.OK, "Entrada autorizada.", ticket
