"""Testes de integração do fluxo de pagamento: da compra ao webhook,
passando pela reconciliação. Nunca toca na rede — simula a Debito Pay com
unittest.mock.patch em requests.post, como payments/tests_debitopay.py."""

import hashlib
import hmac
import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from catalog.models import Event, Price
from partners.models import ApiKey, User
from payments.models import ProviderEvent
from payments.services import reconcile_pending
from ticketing.models import Ticket
from ticketing.services import check_in, confirm_payment, release

WEBHOOK = "/back/payments/webhooks/debitopay"

DEBITOPAY_TEST = {
    "BASE_URL": "https://gyqoaningqhurhvdugne.supabase.co/functions/v1",
    "SECRET_KEY": "sk_sandbox_teste",
    "WEBHOOK_SECRET": "webhook-secret-teste",
    "SIGNATURE_HEADER": "X-Webhook-Signature",
    "MERCHANT_ID": "11111111-1111-1111-1111-111111111111",
    "WALLETS": {
        "mpesa": "12345", "emola": "22222", "mkesh": "33333",
        "visa_mastercard": "44444", "payfast": "55555",
    },
    "DEFAULT_METHOD": "mpesa",
    "TIMEOUT": 30,
}


def _post_resp(payload: dict, ok: bool = True) -> Mock:
    m = Mock()
    m.ok = ok
    m.status_code = 200 if ok else 400
    m.json.return_value = payload
    return m


def _sign(body: bytes) -> str:
    return hmac.new(DEBITOPAY_TEST["WEBHOOK_SECRET"].encode(), body, hashlib.sha256).hexdigest()


def _webhook_body(event_type: str, payment_id: str, amount=None, currency="MZN") -> bytes:
    data = {"payment_id": payment_id, "currency": currency}
    if amount is not None:
        data["amount"] = str(amount)
    return json.dumps({"event": event_type, "data": data}).encode()


@override_settings(DEBITOPAY=DEBITOPAY_TEST)
class Base(TestCase):
    def setUp(self):
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

    @patch("payments.debitopay.requests.post")
    def comprar(self, post, phone="258841111111", payment_id=None):
        """Compra um bilhete com a cobrança mockada a ficar `pending` (como
        e-Mola/mKesh/cartão), para os testes de webhook controlarem o
        desfecho. O `payment_id` é único por omissão, para nunca colidir
        entre bilhetes do mesmo teste."""
        payment_id = payment_id or f"pay_{phone}_{Price.objects.count()}_{id(self)}"
        post.return_value = _post_resp({
            "success": True, "payment_id": payment_id, "payment_method": "emola",
            "status": "pending", "reference": payment_id,
        })
        r = self.api.post("/back/borrow/external/tickets",
                          {"priceId": self.price.id, "eventId": self.event.id,
                           "phone": phone, "paymentMethod": "emola"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return Ticket.objects.get(pk=r.data["data"]["id"])

    def enviar(self, body: bytes) -> "HttpResponse":
        return self.client.post(WEBHOOK, data=body, content_type="application/json",
                                HTTP_X_WEBHOOK_SIGNATURE=_sign(body))


class CobrancaTests(Base):
    def test_compra_abre_cobranca_no_gateway(self):
        t = self.comprar()
        self.assertEqual(t.provider, "debitopay")
        self.assertTrue(t.provider_charge_id)
        self.assertEqual(t.payment, Ticket.Payment.PENDING)

    def test_bilhete_nasce_com_prazo(self):
        self.assertIsNotNone(self.comprar().expires_at)


class WebhookTests(Base):
    def test_webhook_valido_confirma(self):
        t = self.comprar()
        body = _webhook_body("payment.completed", t.provider_charge_id, amount=t.amount)
        r = self.enviar(body)
        t.refresh_from_db()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(t.payment, Ticket.Payment.PAID)

    def test_assinatura_invalida_recusa_e_nao_toca_na_bd(self):
        t = self.comprar()
        body = _webhook_body("payment.completed", t.provider_charge_id, amount=t.amount)
        r = self.client.post(WEBHOOK, data=body, content_type="application/json",
                             HTTP_X_WEBHOOK_SIGNATURE="assinaturafalsa")
        t.refresh_from_db()
        self.assertEqual(r.status_code, 401)
        self.assertEqual(t.payment, Ticket.Payment.PENDING)
        self.assertEqual(ProviderEvent.objects.count(), 0)

    def test_webhook_repetido_e_ignorado(self):
        t = self.comprar()
        body = _webhook_body("payment.completed", t.provider_charge_id, amount=t.amount)
        self.enviar(body)
        self.enviar(body)
        self.assertEqual(ProviderEvent.objects.count(), 1)
        # 2 tentativas são esperadas e corretas: 1 ao abrir a cobrança
        # (start_payment, pending) + 1 ao confirmar (confirm_payment). O que
        # a idempotência impede é uma TERCEIRA, vinda do webhook repetido.
        self.assertEqual(t.attempts.count(), 2)

    def test_valor_adulterado_fica_em_revisao(self):
        t = self.comprar()
        body = _webhook_body("payment.completed", t.provider_charge_id, amount=Decimal("1.00"))
        r = self.enviar(body)
        t.refresh_from_db()
        self.assertEqual(t.payment, Ticket.Payment.REVIEW)
        self.assertIn("divergente", r.data["message"])

    def test_pagamento_falhado_liberta_a_vaga(self):
        t = self.comprar()
        antes = Price.objects.get(pk=self.price.pk).available
        body = _webhook_body("payment.failed", t.provider_charge_id)
        self.enviar(body)
        self.assertEqual(Price.objects.get(pk=self.price.pk).available, antes + 1)

    def test_cobranca_desconhecida_nao_rebenta(self):
        body = _webhook_body("payment.completed", "chg_naoexiste", amount=Decimal("300.00"))
        r = self.enviar(body)
        self.assertEqual(r.status_code, 200)
        self.assertIn("desconhecida", r.data["message"])

    def test_pagamento_tardio_com_vaga_confirma_sem_rebentar(self):
        """O bug original: a reserva já expirou, confirm_payment recusava e
        o webhook respondia 500. Agora tenta reservar de novo e confirma."""
        t = self.comprar()
        release(t, Ticket.Payment.FAILED)   # simula o cron de expiração
        t.refresh_from_db()
        self.assertEqual(t.payment, Ticket.Payment.FAILED)

        body = _webhook_body("payment.completed", t.provider_charge_id, amount=t.amount)
        r = self.enviar(body)
        t.refresh_from_db()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(t.payment, Ticket.Payment.PAID)

    def test_pagamento_tardio_sem_vaga_fica_em_revisao_sem_rebentar(self):
        self.price.quantity_total = 2   # para esgotar o lote com só 2 bilhetes
        self.price.save(update_fields=["quantity_total"])

        t1 = self.comprar(phone="258841111111")
        confirm_payment(t1, provider="debitopay", provider_reference="ref1")
        t2 = self.comprar(phone="258842222222")
        release(t2, Ticket.Payment.FAILED)
        t3 = self.comprar(phone="258843333333")
        confirm_payment(t3, provider="debitopay", provider_reference="ref3")

        body = _webhook_body("payment.completed", t2.provider_charge_id, amount=t2.amount)
        r = self.enviar(body)
        t2.refresh_from_db()
        self.assertEqual(r.status_code, 200)   # nunca 500, mesmo sem vaga
        self.assertEqual(t2.payment, Ticket.Payment.REVIEW)

    def test_reembolso_anula_bilhete_pago_e_liberta_vaga(self):
        t = self.comprar()
        confirm_payment(t, provider="debitopay", provider_reference="ref1")
        antes = Price.objects.get(pk=self.price.pk).available

        body = _webhook_body("payment.refunded", t.provider_charge_id)
        r = self.enviar(body)
        t.refresh_from_db()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(t.payment, Ticket.Payment.REFUNDED)
        self.assertEqual(t.status, Ticket.Status.CANCELLED)
        self.assertEqual(Price.objects.get(pk=self.price.pk).available, antes + 1)

        result, _, _ = check_in(qr_value=t.qr_value, staff_user=self.org)
        self.assertEqual(result, "not_paid")

    def test_chargeback_tem_o_mesmo_efeito_que_reembolso(self):
        t = self.comprar()
        confirm_payment(t, provider="debitopay", provider_reference="ref1")

        body = _webhook_body("payment.chargeback", t.provider_charge_id)
        self.enviar(body)
        t.refresh_from_db()
        self.assertEqual(t.payment, Ticket.Payment.REFUNDED)


@patch("payments.debitopay.requests.post")
class ReconciliacaoTests(Base):
    def test_recupera_webhook_perdido(self, post):
        t = self.comprar()
        post.return_value = _post_resp({
            "success": True,
            "payment": {"id": t.provider_charge_id, "status": "success",
                       "payment_method": "emola", "amount": float(t.amount),
                       "currency": t.currency},
        })
        stats = reconcile_pending()
        t.refresh_from_db()
        self.assertEqual(t.payment, Ticket.Payment.PAID)
        self.assertEqual(stats["confirmados"], 1)

    def test_nao_confirma_o_que_continua_pendente(self, post):
        t = self.comprar()
        post.return_value = _post_resp({
            "success": True,
            "payment": {"id": t.provider_charge_id, "status": "pending",
                       "payment_method": "emola", "amount": float(t.amount),
                       "currency": t.currency},
        })
        reconcile_pending()
        t.refresh_from_db()
        self.assertEqual(t.payment, Ticket.Payment.PENDING)

    def test_valor_divergente_na_reconciliacao_fica_em_revisao(self, post):
        t = self.comprar()
        post.return_value = _post_resp({
            "success": True,
            "payment": {"id": t.provider_charge_id, "status": "success",
                       "payment_method": "emola", "amount": 999.00,
                       "currency": t.currency},
        })
        stats = reconcile_pending()
        t.refresh_from_db()
        self.assertEqual(t.payment, Ticket.Payment.REVIEW)
        self.assertEqual(stats["erros"], 1)
