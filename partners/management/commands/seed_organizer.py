"""Cria apenas um organizador e emite uma chave de API para testes."""

from django.core.management.base import BaseCommand
from django.db import transaction

from partners.models import ApiKey, User


class Command(BaseCommand):
    help = "Popula a base de dados com um organizador de demonstração e emite uma chave de API."

    def add_arguments(self, parser):
        parser.add_argument("--username", default="broto")
        parser.add_argument("--email", default="organizador@example.com")
        parser.add_argument("--password", default="Pa$$w0rd!123")
        parser.add_argument("--company", default="Run With Broto")
        parser.add_argument("--label", default="site parceiro")
        parser.add_argument(
            "--reset", action="store_true",
            help="Apaga o organizador existente antes de criar.",
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
            company_name=o["company"], is_staff=True,
        )
        key, raw = ApiKey.issue(user, label=o["label"])

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Organizador criado."))
        self.stdout.write(f"  utilizador : {user.username} / {o['password']}")
        self.stdout.write(f"  API_KEY    : {raw}")
        self.stdout.write("")
        self.stdout.write(self.style.WARNING(
            "Guarde a API_KEY agora — não voltará a ser mostrada."
        ))
