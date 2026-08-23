"""Cria um organizador, um evento, um lote e uma chave de API para testes."""

from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from catalog.models import Event, Price
from partners.models import ApiKey, User


class Command(BaseCommand):
    help = "Popula a base de dados com dados de demonstração e emite uma chave de API."

    def add_arguments(self, parser):
        parser.add_argument("--username", default="broto")
        parser.add_argument("--email", default="organizador@example.com")
        parser.add_argument("--password", default="Pa$$w0rd!123")
        parser.add_argument("--vagas", type=int, default=5)
        parser.add_argument(
            "--reset", action="store_true",
            help="Apaga o organizador existente e os seus dados antes de criar.",
        )

    @transaction.atomic
    def handle(self, *args, **o):
        if o["reset"]:
            User.objects.filter(username=o["username"]).delete()

        user = User.objects.filter(username=o["username"]).first()
        if user:
            self.stdout.write(self.style.WARNING(
                f"O utilizador '{o['username']}' já existe. "
                f"Use --reset para recomeçar, ou --username outro."
            ))
            return

        user = User.objects.create_user(
            o["username"], email=o["email"], password=o["password"],
            company_name="Run With Broto", is_staff=True,
        )
        event = Event.objects.create(
            organizer=user,
            name="Last Winter Social",
            description="Corrida de 5 km seguida de after run.",
            category="social_run",
            date=timezone.now() + timedelta(days=30),
            image_url="https://example.com/lastwinter.jpeg",
            province="Maputo",
            location_details="Noctis",
            status=Event.Status.PUBLISHED,
        )
        Price.objects.create(
            event=event, name="Inscrição", amount=Decimal("300.00"),
            quantity_total=o["vagas"],
        )
        key, raw = ApiKey.issue(user, label="site parceiro")

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Dados de demonstração criados."))
        self.stdout.write(f"  utilizador : {user.username} / {o['password']}")
        self.stdout.write(f"  EVENT_ID   : {event.id}")
        self.stdout.write(f"  API_KEY    : {raw}")
        self.stdout.write("")
        self.stdout.write(self.style.WARNING(
            "Guarde a API_KEY agora — não voltará a ser mostrada."
        ))
