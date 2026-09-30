import hashlib
import hmac
from django.conf import settings
from django.db import models
from django.utils import timezone

from catalog.models import Price, make_id


def _qr_signature(ticket_id: str) -> str:
    sig = hmac.new(settings.QR_SIGNING_KEY.encode(), ticket_id.encode(), hashlib.sha256)
    return sig.hexdigest()[:16]


class Ticket(models.Model):
    class Status(models.TextChoices):
        VALID = "valid", "Válido"
        CANCELLED = "cancelled", "Cancelado"
        EXPIRED = "expired", "Expirado"

    class Payment(models.TextChoices):
        PENDING = "pending", "Pendente"
        PAID = "paid", "Pago"
        FAILED = "failed", "Falhou"
        REFUNDED = "refunded", "Reembolsado"
        INVITED = "invited", "Convite"
        REVIEW = "review", "Em revisão"
        # Pré-inscrição: ocupa vaga até ao prazo, mas ainda não dá entrada.
        PREREGISTERED = "preregistered", "Pré-inscrito"

    ENTRY_ALLOWED = {"paid", "invited"}

    id = models.CharField(primary_key=True, max_length=40, editable=False)
    price = models.ForeignKey(Price, on_delete=models.PROTECT, related_name="tickets")
    issued_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="issued_tickets"
    )  # o parceiro que emitiu, via chave de API

    # valor congelado na emissão: alterar o preço do lote não muda bilhetes já vendidos
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    currency = models.CharField(max_length=3, default="MZN")

    phone = models.CharField(max_length=20, db_index=True)
    full_name = models.CharField(max_length=200, blank=True)
    email = models.EmailField(blank=True)
    note = models.CharField(
        max_length=255, blank=True,
        help_text="ex.: Patrocinador Coca-Cola",
    )

    # Id do pedido do lado do parceiro (opcional). Serve só para dedupe: se o
    # mesmo POST /tickets chegar duas vezes com o mesmo external_reference
    # (retry, duplo-clique), devolvemos o ticket já existente em vez de criar
    # outro e reservar outra vaga. Ver ticketing/services.py:create_ticket.
    # Vazio ("") não conta como duplicado — só é único quando preenchido.
    external_reference = models.CharField(max_length=100, blank=True, default="")

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.VALID)
    payment = models.CharField(max_length=20, choices=Payment.choices, default=Payment.PENDING)
    payment_method = models.CharField(max_length=40, blank=True)
    # True quando a ApiKey que criou o bilhete é "test": a cobrança usa a
    # sandbox da Debito Pay (settings.DEBITOPAY_SANDBOX), nunca a conta live.
    test_mode = models.BooleanField(default=False)
    provider = models.CharField(max_length=40, blank=True)
    provider_charge_id = models.CharField(max_length=128, blank=True, db_index=True)
    checkout_url = models.URLField(blank=True)
    entered = models.BooleanField(default=False)
    entered_at = models.DateTimeField(null=True, blank=True)

    expires_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["payment", "expires_at"]),
            models.Index(fields=["phone"]),
            models.Index(fields=["issued_to", "external_reference"]),
        ]
        constraints = [
            # Rede de segurança contra corrida entre pedidos concorrentes com
            # o mesmo external_reference. A serialização "normal" já acontece
            # via select_for_update() no Price em create_ticket(); isto só
            # entra em jogo se dois pedidos concorrentes usarem o mesmo
            # external_reference para price_ids diferentes — nesse caso é um
            # erro do lado do parceiro, e o segundo pedido falha com 500 em
            # vez de silenciosamente criar dois tickets.
            #
            # Só cobre tickets "vivos" (pending/paid/preregistered), tal como o dedupe em
            # create_ticket(): um ticket failed/expired/refunded/review já
            # libertou a vaga e não pode bloquear um novo pedido do parceiro
            # com a mesma referência (senão o retry dava 500).
            models.UniqueConstraint(
                fields=["issued_to", "external_reference"],
                condition=~models.Q(external_reference="")
                & models.Q(payment__in=["pending", "paid", "preregistered"]),
                name="uniq_ticket_partner_external_reference",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self.id:
            self.id = make_id("TCKT")
        super().save(*args, **kwargs)

    @property
    def event(self):
        return self.price.event

    @property
    def qr_value(self) -> str:
        """`TCKT…|assinatura` — o porteiro valida sem confiar num ID adivinhável."""
        return f"{self.id}|{_qr_signature(self.id)}"

    def to_api(self) -> dict:
        event = self.price.event
        return {
            "id": self.id,
            "eventId": self.price.event_id,
            "event": {
                "id": event.id,
                "name": event.name,
                "category": event.category,
                "date": event.date.isoformat().replace("+00:00", "Z"),
                "imageUrl": event.image_url,
                "location": {"province": event.province, "details": event.location_details},
            },
            "priceId": self.price_id,
            "price": {**self.price.to_api(), "amount": float(self.amount)},
            "amount": float(self.amount),
            "currency": self.currency,
            "phone": self.phone,
            "fullName": self.full_name,
            "email": self.email,
            "note": self.note,
            "externalReference": self.external_reference,
            "isInvite": self.payment == self.Payment.INVITED,
            "status": self.status,
            "payment": self.payment,
            "paymentMethod": self.payment_method,
            # Limite da reserva (pagamento pendente ou pré-inscrição por confirmar).
            "expiresAt": (
                self.expires_at.isoformat().replace("+00:00", "Z") if self.expires_at else None
            ),
            "checkoutUrl": self.checkout_url,
            "entered": self.entered,
            "qrValue": self.qr_value,
            "createdAt": self.created_at.isoformat().replace("+00:00", "Z"),
            "updatedAt": self.updated_at.isoformat().replace("+00:00", "Z"),
        }

    def __str__(self):
        return f"{self.id} - {self.phone}"


class PaymentAttempt(models.Model):
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="attempts")
    provider = models.CharField(max_length=40)          # mpesa, emola, card...
    provider_reference = models.CharField(max_length=128, blank=True, db_index=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    succeeded = models.BooleanField(default=False)
    raw_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class CheckInLog(models.Model):
    class Result(models.TextChoices):
        OK = "ok", "Entrada autorizada"
        ALREADY_ENTERED = "already_entered", "Já tinha entrado"
        NOT_PAID = "not_paid", "Pagamento pendente"
        NOT_FOUND = "not_found", "Bilhete não encontrado"
        INVALID_QR = "invalid_qr", "QR inválido"

    ticket_id_raw = models.CharField(max_length=100, db_index=True)
    result = models.CharField(max_length=20, choices=Result.choices)
    scanned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="scans"
    )
    scanned_at = models.DateTimeField(auto_now_add=True)
    raw_qr = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-scanned_at"]


class PartnerDelivery(models.Model):
    """Fila de avisos ao parceiro. `notify_partner()` só cria esta linha,
    dentro da mesma transação que confirma o pagamento (ou o reembolso) —
    nunca prende o pedido do webhook da Debito Pay à espera da resposta do
    site do parceiro, e nunca perde o aviso se o parceiro estiver em baixo.
    A entrega de facto corre à parte, no comando `deliver_webhooks`.
    """

    class Event(models.TextChoices):
        TICKET_PAID = "ticket.paid", "Bilhete pago"
        TICKET_REFUNDED = "ticket.refunded", "Bilhete reembolsado"

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="partner_deliveries")
    event = models.CharField(max_length=30, choices=Event.choices)
    payload = models.JSONField(default=dict, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    delivered_at = models.DateTimeField(null=True, blank=True)
    gave_up_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["next_attempt_at"]
        indexes = [
            models.Index(fields=["delivered_at", "gave_up_at", "next_attempt_at"]),
        ]

    def __str__(self):
        return f"{self.ticket_id} — {self.event} (#{self.pk})"