"""Percorre o fluxo de pagamento completo com o gateway falso.

    python manage.py test_payment_flow

Não precisa de credenciais: força PAYMENT_PROVIDER=fake internamente.
"""

from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.test import Client as DjangoClient
from django.utils import timezone
from rest_framework.test import APIClient

from catalog.models import Event, Price
from partners.models import ApiKey, User
from payments.models import ProviderEvent
from payments.providers.base import FAILED, SUCCEEDED
from payments.providers.registry import get_provider
from payments.services import reconcile_pending
from ticketing.models import Ticket

WEBHOOK = "/back/payments/webhooks/debitopay"


class Command(BaseCommand):
    help = "Testa o fluxo de pagamento de ponta a ponta com o gateway falso."

    def handle(self, *args, **options):
        stamp = int(timezone.now().timestamp())
        org = User.objects.create_user(
            f"pgtest{stamp}", email=f"pg{stamp}@test.local", password="Pa$$w0rd!123"
        )
        event = Event.objects.create(
            organizer=org, name="Evento de teste", category="test",
            date=timezone.now() + timedelta(days=30), province="Maputo",
            status=Event.Status.PUBLISHED,
        )
        price = Price.objects.create(
            event=event, name="Geral", amount=Decimal("300.00"), quantity_total=5
        )
        _, raw_key = ApiKey.issue(org, label="teste")

        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {raw_key}")
        gw = get_provider("fake")
        http = DjangoClient()

        def buy(phone):
            r = api.post("/back/borrow/external/tickets",
                         {"priceId": price.id, "eventId": event.id, "phone": phone},
                         format="json")
            if r.status_code != 201:
                raise CommandError(f"compra falhou: {r.status_code} {r.data}")
            return r.data["data"]

        def send(body, signature):
            return http.post(WEBHOOK, data=body, content_type="application/json",
                             HTTP_X_DEBITO_SIGNATURE=signature)

        def check(label, condition, detail=""):
            style = self.style.SUCCESS if condition else self.style.ERROR
            mark = "OK  " if condition else "FALHA"
            self.stdout.write(style(f"  {mark} {label}") + (f"  {detail}" if detail else ""))
            if not condition:
                raise CommandError(f"verificação falhou: {label}")

        self.stdout.write("\nFluxo de pagamento (gateway falso)\n")

        t1 = buy("258841111111")
        tk1 = Ticket.objects.get(pk=t1["id"])
        check("bilhete criado como pendente", t1["payment"] == "pending", t1["id"])
        check("cobrança aberta no gateway", bool(tk1.provider_charge_id),
              tk1.provider_charge_id)

        body, hdr = gw.build_webhook(tk1.provider_charge_id, SUCCEEDED)
        send(body, hdr["X-Debito-Signature"])
        tk1.refresh_from_db()
        check("webhook confirma o pagamento", tk1.payment == "paid")

        antes = ProviderEvent.objects.count()
        send(body, hdr["X-Debito-Signature"])
        check("webhook repetido é ignorado", ProviderEvent.objects.count() == antes)

        r = send(body, "assinaturafalsa")
        check("assinatura falsa é recusada", r.status_code == 401)

        t2 = buy("258842222222")
        tk2 = Ticket.objects.get(pk=t2["id"])
        body, hdr = gw.build_webhook(tk2.provider_charge_id, SUCCEEDED,
                                     amount=Decimal("1.00"))
        send(body, hdr["X-Debito-Signature"])
        tk2.refresh_from_db()
        check("valor adulterado não confirma", tk2.payment == "pending")

        t3 = buy("258843333333")
        tk3 = Ticket.objects.get(pk=t3["id"])
        vagas = Price.objects.get(pk=price.id).available
        body, hdr = gw.build_webhook(tk3.provider_charge_id, FAILED)
        send(body, hdr["X-Debito-Signature"])
        check("pagamento falhado liberta a vaga",
              Price.objects.get(pk=price.id).available == vagas + 1)

        t4 = buy("258844444444")
        tk4 = Ticket.objects.get(pk=t4["id"])
        gw.settle(tk4.provider_charge_id, SUCCEEDED)   # pagou, webhook perdeu-se
        stats = reconcile_pending()
        tk4.refresh_from_db()
        check("reconciliação recupera webhook perdido", tk4.payment == "paid", str(stats))

        self.stdout.write(self.style.SUCCESS("\nTodas as verificações passaram.\n"))
