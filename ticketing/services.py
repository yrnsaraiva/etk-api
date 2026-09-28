"""Regras de negócio. O ponto crítico é não vender mais bilhetes do que existem."""

import hmac
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from catalog.models import Event, Price, make_id

from .models import CheckInLog, PaymentAttempt, Ticket, _qr_signature


class TicketError(ValidationError):
    pass


# Estados em que um ticket antigo com o mesmo external_reference ainda
# "conta" como o mesmo pedido em curso. FAILED/REFUNDED já libertaram a vaga
# e não bloqueiam um pedido novo.
_ALIVE_FOR_DEDUPE = (Ticket.Payment.PENDING, Ticket.Payment.PAID)


@transaction.atomic
def create_ticket(*, price_id: str, event_id: str, phone: str, issued_to,
                  full_name: str = "", email: str = "", payment_method: str = "",
                  external_reference: str = "") -> Ticket:
    """Emite um bilhete `pending` e reserva o lugar.

    `select_for_update()` tranca a linha do Price até ao fim da transação. Sem
    isto, dois pedidos simultâneos leem "resta 1", ambos passam na verificação
    e vendem-se dois bilhetes para uma vaga. Essa mesma trava serializa também
    a verificação de `external_reference` abaixo: dois pedidos concorrentes
    para o mesmo price_id com a mesma external_reference não passam ambos —
    o segundo só continua depois do primeiro ter commitado, e nessa altura já
    encontra o ticket do primeiro.

    Se `external_reference` vier preenchida e já existir um ticket vivo
    (pending ou paid) com essa referência para este parceiro, devolve esse
    ticket em vez de criar outro — é o que evita duplicados quando o parceiro
    repete o POST (timeout do lado dele, duplo clique, etc.).
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

    if external_reference:
        existing = Ticket.objects.filter(
            issued_to=issued_to, external_reference=external_reference,
            payment__in=_ALIVE_FOR_DEDUPE,
        ).first()
        if existing:
            return existing

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
        external_reference=external_reference,
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
def reclaim_and_confirm(ticket: Ticket, *, provider: str, provider_reference: str,
                        payload: dict | None = None) -> Ticket:
    """Confirma um pagamento que chegou depois de a reserva ter expirado (ou de
    uma tentativa anterior ter ficado em revisão).

    O dinheiro já foi aceite pelo gateway — a única coisa que ainda está em
    aberto é se há vaga para o bilhete. Tenta reservar de novo uma vaga no
    mesmo lote, com o mesmo lock de `create_ticket`; se houver, confirma como
    `paid`. Se não houver, fica `review` para decisão manual no /admin/ — mas
    o pagamento nunca é ignorado nem perdido.
    """
    ticket = Ticket.objects.select_for_update().select_related("price").get(pk=ticket.pk)

    if ticket.payment == Ticket.Payment.PAID:
        return ticket
    if ticket.payment not in (Ticket.Payment.FAILED, Ticket.Payment.REVIEW):
        raise TicketError(f"Bilhete em estado '{ticket.payment}'.")

    price = Price.objects.select_for_update().get(pk=ticket.price_id)

    PaymentAttempt.objects.create(
        ticket=ticket, provider=provider, provider_reference=provider_reference,
        amount=ticket.amount, succeeded=True, raw_payload=payload or {},
    )

    if price.available > 0:
        Price.objects.filter(pk=price.pk).update(quantity_reserved=F("quantity_reserved") + 1)
        if price.available - 1 <= 0:
            Price.objects.filter(pk=price.pk).update(status=Price.Status.SOLD_OUT)
        ticket.payment = Ticket.Payment.PAID
        ticket.status = Ticket.Status.VALID
        ticket.paid_at = timezone.now()
        ticket.expires_at = None
        ticket.save(update_fields=["payment", "status", "paid_at", "expires_at", "updated_at"])
    else:
        ticket.payment = Ticket.Payment.REVIEW
        ticket.save(update_fields=["payment", "updated_at"])
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


@transaction.atomic
def refund(ticket: Ticket) -> Ticket:
    """Reembolso ou chargeback: anula o bilhete.

    Cobre dois pontos de entrada — o webhook `payment.refunded`/
    `payment.chargeback` (ticket `paid`) e a ação de admin "Marcar para
    reembolso" sobre um ticket `review` (nunca chegou a reservar vaga, por
    isso não há nada a devolver ao lote). Idempotente: um ticket já
    `refunded` ou nalgum outro estado não sofre nada.
    """
    ticket = Ticket.objects.select_for_update().select_related("price__event").get(pk=ticket.pk)
    if ticket.payment not in (Ticket.Payment.PAID, Ticket.Payment.REVIEW):
        return ticket

    ocupava_vaga = ticket.payment == Ticket.Payment.PAID
    ticket.payment = Ticket.Payment.REFUNDED
    ticket.status = Ticket.Status.CANCELLED
    ticket.save(update_fields=["payment", "status", "updated_at"])

    if ocupava_vaga and ticket.event.date > timezone.now():
        Price.objects.filter(pk=ticket.price_id).update(
            quantity_reserved=F("quantity_reserved") - 1
        )
        Price.objects.filter(pk=ticket.price_id, status=Price.Status.SOLD_OUT).update(
            status=Price.Status.ACTIVE
        )
    return ticket


@transaction.atomic
def issue_invites(*, price_id: str, event_id: str, organizer, quantity: int = 1,
                  holder_name: str = "", holder_email: str = "", phone: str = "",
                  note: str = "") -> list[Ticket]:
    """Emite `quantity` bilhetes gratuitos para o mesmo lote — convites do
    organizador a patrocinadores, parceiros, imprensa, etc.

    Reutiliza o mesmo lock e a mesma reserva de stock do `create_ticket`: um
    convite ocupa um lugar tal como um bilhete pago, e não pode fazer o lote
    ultrapassar a capacidade. A diferença é que nunca passa pelo gateway de
    pagamento — nasce já `invited`, pronto para entrar.

    Ao contrário da compra, não exige que o evento esteja `PUBLISHED`: o
    organizador pode querer convidar patrocinadores antes de abrir a venda.
    """
    if quantity < 1:
        raise TicketError("A quantidade tem de ser pelo menos 1.")

    try:
        price = (
            Price.objects.select_for_update().select_related("event").get(pk=price_id)
        )
    except Price.DoesNotExist:
        raise TicketError("priceId inválido.")

    if price.event_id != event_id or price.event.organizer_id != organizer.pk:
        raise TicketError("O priceId não pertence a este eventId.")
    if price.available < quantity:
        raise TicketError(f"Só restam {price.available} vaga(s) neste lote.")

    Price.objects.filter(pk=price.pk).update(
        quantity_reserved=F("quantity_reserved") + quantity
    )
    if price.available - quantity <= 0:
        Price.objects.filter(pk=price.pk).update(status=Price.Status.SOLD_OUT)

    ids_usados: set[str] = set()
    tickets = []
    for _ in range(quantity):
        novo_id = make_id("TCKT")
        while novo_id in ids_usados:      # colisão dentro do próprio lote
            novo_id = make_id("TCKT")
        ids_usados.add(novo_id)
        tickets.append(Ticket(
            id=novo_id, price=price, issued_to=organizer, phone=phone,
            full_name=holder_name, email=holder_email, note=note, amount=0,
            currency=price.currency, payment_method="invite",
            payment=Ticket.Payment.INVITED,
        ))
    Ticket.objects.bulk_create(tickets)
    return tickets


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
    """Aceita só `TCKT…|assinatura`, verificando o HMAC. Sem assinatura = inválido:
    com IDs previsíveis, aceitar `TCKT…` nu deixava entrar quem adivinhasse o ID
    de um bilhete pago."""
    ticket_id, sep, sig = (qr_value or "").strip().partition("|")
    if not ticket_id.startswith("TCKT") or not sep:
        return None
    expected = _qr_signature(ticket_id)
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

    if ticket.payment not in Ticket.ENTRY_ALLOWED:
        log(CheckInLog.Result.NOT_PAID, ticket_id)
        return CheckInLog.Result.NOT_PAID, f"Pagamento não confirmado ({ticket.payment}).", ticket

    if ticket.status != Ticket.Status.VALID:
        log(CheckInLog.Result.NOT_PAID, ticket_id)
        return CheckInLog.Result.NOT_PAID, f"Bilhete {ticket.get_status_display().lower()}.", ticket

    if ticket.entered:
        log(CheckInLog.Result.ALREADY_ENTERED, ticket_id)
        when = timezone.localtime(ticket.entered_at).strftime("%H:%M")
        return CheckInLog.Result.ALREADY_ENTERED, f"Já entrou às {when}.", ticket

    ticket.entered = True
    ticket.entered_at = timezone.now()
    ticket.save(update_fields=["entered", "entered_at", "updated_at"])
    log(CheckInLog.Result.OK, ticket_id)
    return CheckInLog.Result.OK, "Entrada autorizada.", ticket