"""Fluxo de pagamento completo com o gateway falso.
Correr: PAYMENT_PROVIDER=fake python manage.py shell < payments_test.py"""
from datetime import timedelta
from decimal import Decimal

from django.test import Client as DjangoClient
from django.utils import timezone
from rest_framework.test import APIClient

from partners.models import ApiKey, User
from catalog.models import Event, Price
from ticketing.models import Ticket
from payments.models import ProviderEvent
from payments.providers.registry import get_provider
from payments.services import reconcile_pending
from payments.providers.base import SUCCEEDED, FAILED

org = User.objects.create_user("broto", email="o@x.com", password="Pa$$w0rd!123",
                               company_name="Run With Broto")
ev = Event.objects.create(organizer=org, name="Last Winter Social", category="social_run",
                          date=timezone.now() + timedelta(days=30), province="Maputo",
                          location_details="Noctis", status=Event.Status.PUBLISHED)
pr = Price.objects.create(event=ev, name="Inscrição", amount=Decimal("300.00"), quantity_total=5)
key, raw = ApiKey.issue(org, label="site")

c = APIClient()
c.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
gw = get_provider("fake")
WH = "/back/payments/webhooks/debitopay"
dj = DjangoClient()

def buy(phone):
    r = c.post("/back/borrow/external/tickets",
               {"priceId": pr.id, "eventId": ev.id, "phone": phone}, format="json")
    assert r.status_code == 201, r.data
    return r.data["data"]

t1 = buy("258841111111")
print("1. bilhete criado  :", t1["id"], "| payment:", t1["payment"],
      "|", t1["paymentInstructions"])
tk = Ticket.objects.get(pk=t1["id"])
print("2. cobranca no gw  :", tk.provider, tk.provider_charge_id)
assert tk.provider_charge_id

body, hdr = gw.build_webhook(tk.provider_charge_id, SUCCEEDED)
r = dj.post(WH, data=body, content_type="application/json", **{"HTTP_X_DEBITO_SIGNATURE": hdr["X-Debito-Signature"]})
print("3. webhook         :", r.status_code, r.json()["message"])
tk.refresh_from_db(); assert tk.payment == "paid"

r = dj.post(WH, data=body, content_type="application/json", **{"HTTP_X_DEBITO_SIGNATURE": hdr["X-Debito-Signature"]})
print("4. webhook repetido:", r.json()["message"], "| eventos guardados:", ProviderEvent.objects.count())
assert ProviderEvent.objects.count() == 1

r = dj.post(WH, data=body, content_type="application/json", **{"HTTP_X_DEBITO_SIGNATURE": "assinaturafalsa"})
print("5. assinatura falsa:", r.status_code, r.json()["message"])
assert r.status_code == 401

# --- fraude: webhook autentico mas com valor adulterado ---
t2 = buy("258842222222")
tk2 = Ticket.objects.get(pk=t2["id"])
body, hdr = gw.build_webhook(tk2.provider_charge_id, SUCCEEDED, amount=Decimal("1.00"))
r = dj.post(WH, data=body, content_type="application/json", **{"HTTP_X_DEBITO_SIGNATURE": hdr["X-Debito-Signature"]})
tk2.refresh_from_db()
print("6. valor adulterado:", r.json()["message"], "| payment:", tk2.payment)
assert tk2.payment == "pending"

# --- pagamento falhado liberta a vaga ---
t3 = buy("258843333333")
tk3 = Ticket.objects.get(pk=t3["id"])
antes = Price.objects.get(pk=pr.id).available
body, hdr = gw.build_webhook(tk3.provider_charge_id, FAILED)
dj.post(WH, data=body, content_type="application/json", **{"HTTP_X_DEBITO_SIGNATURE": hdr["X-Debito-Signature"]})
print("7. pagamento falhou: vaga", antes, "->", Price.objects.get(pk=pr.id).available)
assert Price.objects.get(pk=pr.id).available == antes + 1

# --- webhook perdido: a reconciliacao salva o cliente ---
t4 = buy("258844444444")
tk4 = Ticket.objects.get(pk=t4["id"])
gw.settle(tk4.provider_charge_id, SUCCEEDED)   # pagou no telemovel, webhook nunca chegou
print("8. antes da reconc.:", Ticket.objects.get(pk=tk4.id).payment)
print("9. reconciliacao   :", reconcile_pending())
tk4.refresh_from_db()
print("10. depois         :", tk4.payment)
assert tk4.payment == "paid"

print("\nTUDO OK")
