from django.core.management.base import BaseCommand

from ticketing.webhooks import deliver_pending_webhooks


class Command(BaseCommand):
    help = "Entrega os avisos ao parceiro enfileirados em PartnerDelivery (cron, a cada minuto)."

    def handle(self, *args, **options):
        self.stdout.write(str(deliver_pending_webhooks()))
