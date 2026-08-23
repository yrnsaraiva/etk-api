from django.core.management.base import BaseCommand

from ticketing.services import expire_stale_tickets


class Command(BaseCommand):
    help = "Liberta vagas de bilhetes pendentes que passaram do prazo (cron 1-2 min)."

    def handle(self, *args, **options):
        n = expire_stale_tickets()
        self.stdout.write(f"{n} bilhete(s) expirado(s), vagas libertadas.")
