"""Copie para payments/management/commands/reconcile_payments.py"""
from django.core.management.base import BaseCommand

from payments.services import reconcile_pending


class Command(BaseCommand):
    help = "Sonda o gateway sobre pagamentos pendentes (correr a cada 2-5 min)."

    def handle(self, *args, **options):
        self.stdout.write(str(reconcile_pending()))
