import hashlib
import secrets

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone


class User(AbstractUser):
    """Organizador / parceiro. Cria eventos e emite chaves de API."""

    email = models.EmailField(unique=True)
    company_name = models.CharField(max_length=150, blank=True)
    webhook_url = models.URLField(blank=True)
    webhook_secret = models.CharField(max_length=64, blank=True)

    REQUIRED_FIELDS = ["email"]

    def save(self, *args, **kwargs):
        # Com webhook_secret vazio, a assinatura X-ETK-Signature é calculada
        # com uma chave vazia e não protege nada. Gerar aqui garante que
        # nunca existe webhook_url sem segredo, sem depender de quem cria o
        # registo se lembrar de o fazer.
        if self.webhook_url and not self.webhook_secret:
            self.webhook_secret = secrets.token_urlsafe(32)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.company_name or self.get_username()

    def webhook_endpoints_to_notify(self):
        """Destinos activos dos avisos: o webhook_url do organizador (campo antigo, `None`) mais os WebhookEndpoint."""
        targets = [None] if self.webhook_url else []
        return targets + list(self.webhook_endpoints.filter(is_active=True))


class WebhookEndpoint(models.Model):
    """Destino adicional dos avisos ao parceiro (ticket.paid, ticket.refunded).

    O campo `User.webhook_url` continua a funcionar como o primeiro destino; estes são os restantes — por exemplo
    o site de bilhetes e a app de membros a receber os mesmos avisos, cada um com o seu segredo e a sua fila de
    entrega (um destino em baixo não atrasa os outros).
    """

    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="webhook_endpoints")
    label = models.CharField(max_length=100, blank=True, help_text="Para identificar o destino (ex.: 'App de membros').")
    url = models.URLField()
    secret = models.CharField(max_length=64, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def save(self, *args, **kwargs):
        # Mesma regra do User.webhook_secret: nunca existe destino sem segredo (a assinatura não protegeria nada)
        if not self.secret:
            self.secret = secrets.token_urlsafe(32)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.label or self.url} ({self.owner})"


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


class ApiKey(models.Model):
    """Chave `etk_live_...` usada pelo site do parceiro.

    Guarda-se apenas o hash: se a base de dados vazar, as chaves não são
    utilizáveis. O valor em claro é mostrado uma única vez, na criação.
    """

    class Environment(models.TextChoices):
        LIVE = "live", "Produção"
        TEST = "test", "Teste"

    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="api_keys")
    label = models.CharField(max_length=100, blank=True)
    environment = models.CharField(max_length=10, choices=Environment.choices, default=Environment.LIVE)
    prefix = models.CharField(max_length=16)          # etk_live
    last_four = models.CharField(max_length=4)        # para identificar na UI
    key_hash = models.CharField(max_length=64, unique=True, db_index=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    @classmethod
    def issue(cls, owner, label="", environment=Environment.LIVE):
        raw = f"etk_{environment}_{secrets.token_urlsafe(18)[:24]}"
        key = cls.objects.create(
            owner=owner,
            label=label,
            environment=environment,
            prefix=f"etk_{environment}",
            last_four=raw[-4:],
            key_hash=_hash(raw),
        )
        return key, raw  # raw só existe aqui — nunca mais

    @classmethod
    def resolve(cls, raw: str):
        try:
            key = cls.objects.select_related("owner").get(key_hash=_hash(raw), revoked_at__isnull=True)
        except cls.DoesNotExist:
            return None
        cls.objects.filter(pk=key.pk).update(last_used_at=timezone.now())
        return key

    def revoke(self):
        self.revoked_at = timezone.now()
        self.save(update_fields=["revoked_at"])

    def __str__(self):
        return f"{self.prefix}_…{self.last_four}"
