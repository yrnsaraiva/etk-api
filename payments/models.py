from django.db import models


class ProviderEvent(models.Model):
    """Todo o webhook recebido, para idempotência e auditoria.

    O gateway reenvia em caso de timeout; sem esta tabela, um reenvio marcaria
    o bilhete como pago duas vezes e emitiria dois recibos.
    """

    provider = models.CharField(max_length=40)
    event_id = models.CharField(max_length=128)
    event_type = models.CharField(max_length=80, blank=True)
    charge_reference = models.CharField(max_length=128, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    outcome = models.CharField(max_length=200, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-received_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "event_id"], name="uniq_provider_event"
            )
        ]

    def __str__(self):
        return f"{self.provider}:{self.event_id}"
