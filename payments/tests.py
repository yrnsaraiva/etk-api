import json
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from catalog.models import Event, Price
from partners.models import ApiKey, User
from payments.models import ProviderEvent
from payments.providers import registry
from payments.providers.base import FAILED, SUCCEEDED
from payments.services import reconcile_pending
from ticketing.models import Ticket

WEBHOOK = "/back/payments/webhooks/debitopay"


@override_settings(PAYMENT_PROVIDER="fake")
class Base(TestCase):
    def setUp(self):
        registry.reset()
        self.org = User.objects.create_user(
            "org", email="org@test.local", password="Pa$$w0rd!123"
        )
        self.event = Event.objects.create(
            organizer=self.org, name="Festival",
            date=timezone.now() + timedelta(days=30), status=Event.Status.PUBLISHED,
        )
        self.price = Price.objects.create(
            event=self.event, name="Geral", amount=Decimal("300.00"), quantity_total=5
        )
        _, raw = ApiKey.issue(self.org)
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
        self.gw = registry.get_provider("fake")

    def tearDown(self):
        registry.reset()

    def comprar(self, phone="258841111111"):
        r = self.api.post("/back/borrow/external/tickets",
                          {"priceId": self.price.id, "eventId": self.event.id,
                           "phone": phone}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return Ticket.objects.get(pk=r.data["data"]["id"])

    def enviar(self, body, assinatura):
        return self.client.post(WEBHOOK, data=body, content_type="application/json",
                                HTTP_X_DEBITO_SIGNATURE=assinatura)


class CobrancaTests(Base):
    def test_compra_abre_cobranca_no_gateway(self):
        t = self.comprar()
        self.assertEqual(t.provider, "fake")
        self.assertTrue(t.provider_charge_id)
        self.assertEqual(t.payment, Ticket.Payment.PENDING)

    def test_bilhete_nasce_com_prazo(self):
        self.assertIsNotNone(self.comprar().expires_at)


class WebhookTests(Base):
    def test_webhook_valido_confirma(self):
        t = self.comprar()
        body, hdr = self.gw.build_webhook(t.provider_charge_id, SUCCEEDED)
        r = self.enviar(body, hdr["X-Debito-Signature"])
        t.refresh_from_db()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(t.payment, Ticket.Payment.PAID)

    def test_assinatura_invalida_recusa_e_nao_toca_na_bd(self):
        t = self.comprar()
        body, _ = self.gw.build_webhook(t.provider_charge_id, SUCCEEDED)
        r = self.enviar(body, "assinaturafalsa")
        t.refresh_from_db()
        self.assertEqual(r.status_code, 401)
        self.assertEqual(t.payment, Ticket.Payment.PENDING)
        self.assertEqual(ProviderEvent.objects.count(), 0)

    def test_webhook_repetido_e_ignorado(self):
        t = self.comprar()
        body, hdr = self.gw.build_webhook(t.provider_charge_id, SUCCEEDED)
        self.enviar(body, hdr["X-Debito-Signature"])
        self.enviar(body, hdr["X-Debito-Signature"])
        self.assertEqual(ProviderEvent.objects.count(), 1)
        # 2 tentativas são esperadas e corretas: 1 ao abrir a cobrança
        # (start_payment) + 1 ao confirmar (confirm_payment). O que a
        # idempotência impede é uma TERCEIRA, vinda do webhook repetido.
        self.assertEqual(t.attempts.count(), 2)

    def test_valor_adulterado_nao_confirma(self):
        t = self.comprar()
        body, hdr = self.gw.build_webhook(t.provider_charge_id, SUCCEEDED,
                                          amount=Decimal("1.00"))
        r = self.enviar(body, hdr["X-Debito-Signature"])
        t.refresh_from_db()
        self.assertEqual(t.payment, Ticket.Payment.PENDING)
        self.assertIn("divergente", r.data["message"])

    def test_pagamento_falhado_liberta_a_vaga(self):
        t = self.comprar()
        antes = Price.objects.get(pk=self.price.pk).available
        body, hdr = self.gw.build_webhook(t.provider_charge_id, FAILED)
        self.enviar(body, hdr["X-Debito-Signature"])
        self.assertEqual(Price.objects.get(pk=self.price.pk).available, antes + 1)

    def test_cobranca_desconhecida_nao_rebenta(self):
        payload = {"id": "evt_x", "event": "payment.succeeded",
                   "data": {"id": "chg_naoexiste", "status": "succeeded",
                            "amount": "300.00", "currency": "MZN"}}
        body = json.dumps(payload).encode()
        import hashlib, hmac
        from payments.providers.fake import WEBHOOK_SECRET
        sig = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        r = self.enviar(body, sig)
        self.assertEqual(r.status_code, 200)
        self.assertIn("desconhecida", r.data["message"])


class ReconciliacaoTests(Base):
    def test_recupera_webhook_perdido(self):
        t = self.comprar()
        self.gw.settle(t.provider_charge_id, SUCCEEDED)   # pagou, webhook perdeu-se
        stats = reconcile_pending()
        t.refresh_from_db()
        self.assertEqual(t.payment, Ticket.Payment.PAID)
        self.assertEqual(stats["confirmados"], 1)

    def test_nao_confirma_o_que_continua_pendente(self):
        t = self.comprar()
        reconcile_pending()
        t.refresh_from_db()
        self.assertEqual(t.payment, Ticket.Payment.PENDING)

    def test_valor_divergente_na_reconciliacao_nao_confirma(self):
        t = self.comprar()
        self.gw.settle(t.provider_charge_id, SUCCEEDED)
        Ticket.objects.filter(pk=t.pk).update(amount=Decimal("999.00"))
        stats = reconcile_pending()
        t.refresh_from_db()
        self.assertEqual(t.payment, Ticket.Payment.PENDING)
        self.assertEqual(stats["erros"], 1)
