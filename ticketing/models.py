import hashlib
import hmac
from django.conf import settings
from django.db import models

from catalog.models import Price, make_id


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

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.VALID)
    payment = models.CharField(max_length=20, choices=Payment.choices, default=Payment.PENDING)
    payment_method = models.CharField(max_length=40, blank=True)
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
        sig = hmac.new(settings.SECRET_KEY.encode(), self.id.encode(), hashlib.sha256)
        return f"{self.id}|{sig.hexdigest()[:16]}"

    def to_api(self) -> dict:
        return {
            "id": self.id,
            "eventId": self.price.event_id,
            "priceId": self.price_id,
            "price": {**self.price.to_api(), "amount": float(self.amount)},
            "amount": float(self.amount),
            "currency": self.currency,
            "phone": self.phone,
            "fullName": self.full_name,
            "email": self.email,
            "note": self.note,
            "isInvite": self.payment == self.Payment.INVITED,
            "status": self.status,
            "payment": self.payment,
            "paymentMethod": self.payment_method,
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
