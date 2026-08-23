import random
import time
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone


def make_id(prefix: str) -> str:
    """IDs no formato do contrato: EVNT17827193458075 (prefixo + epoch + 4 dígitos)."""
    return f"{prefix}{int(time.time())}{random.randint(1000, 9999)}"


class Event(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Rascunho"
        PUBLISHED = "published", "Publicado"
        CANCELLED = "cancelled", "Cancelado"

    id = models.CharField(primary_key=True, max_length=40, editable=False)
    organizer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="events"
    )
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    category = models.CharField(max_length=60, blank=True)   # ex.: "social_run"
    date = models.DateTimeField()
    image_url = models.URLField(blank=True)
    province = models.CharField(max_length=60, blank=True)
    location_details = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["date"]
        indexes = [models.Index(fields=["status", "date"])]

    def save(self, *args, **kwargs):
        if not self.id:
            self.id = make_id("EVNT")
        super().save(*args, **kwargs)

    @property
    def total_tickets_purchased(self) -> int:
        from ticketing.models import Ticket
        return Ticket.objects.filter(
            price__event=self, payment=Ticket.Payment.PAID
        ).count()

    def to_api(self) -> dict:
        """Serialização no formato exato que o cliente consome (camelCase)."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "date": self.date.isoformat().replace("+00:00", "Z"),
            "imageUrl": self.image_url,
            "status": self.status,
            "location": {"province": self.province, "details": self.location_details},
            "prices": [p.to_api() for p in self.prices.all()],
            "totalTicketsPurchased": self.total_tickets_purchased,
        }

    def __str__(self):
        return f"{self.id} — {self.name}"


class Price(models.Model):
    """Lote de bilhetes com preço próprio (Geral, VIP, Early bird...)."""

    class Status(models.TextChoices):
        ACTIVE = "active", "Ativo"
        INACTIVE = "inactive", "Inativo"
        SOLD_OUT = "sold_out", "Esgotado"

    id = models.CharField(primary_key=True, max_length=40, editable=False)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="prices")
    name = models.CharField(max_length=100, default="Geral")
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    currency = models.CharField(max_length=3, default=settings.DEFAULT_CURRENCY)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    quantity_total = models.PositiveIntegerField()
    # inclui bilhetes pagos + pendentes ainda dentro do prazo
    quantity_reserved = models.PositiveIntegerField(default=0)
    max_per_request = models.PositiveSmallIntegerField(default=1)
    sales_start = models.DateTimeField(null=True, blank=True)
    sales_end = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["amount", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity_reserved__lte=models.F("quantity_total")),
                name="price_no_oversell",
            )
        ]

    def save(self, *args, **kwargs):
        if not self.id:
            self.id = make_id("PRC")
        super().save(*args, **kwargs)

    @property
    def available(self) -> int:
        return max(self.quantity_total - self.quantity_reserved, 0)

    def is_on_sale(self, now=None) -> bool:
        now = now or timezone.now()
        if self.event.status != Event.Status.PUBLISHED:
            return False
        if self.status != self.Status.ACTIVE:
            return False
        if self.sales_start and now < self.sales_start:
            return False
        if self.sales_end and now > self.sales_end:
            return False
        return self.available > 0

    def to_api(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "amount": float(self.amount),
            "currency": self.currency,
            "status": self.status,
            "available": self.available,
        }

    def __str__(self):
        return f"{self.id} — {self.name} ({self.amount})"
