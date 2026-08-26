"""Testes do adaptador Debito Pay: constrói o pedido certo e interpreta a
resposta certa, sem tocar na rede — usa unittest.mock.patch em requests.post.

Cobrem os dois casos que a documentação real revelou e que o adaptador
genérico anterior não tratava:

1. M-Pesa confirma de forma SÍNCRONA (status "success" já na 1ª resposta).
2. Cada método usa a sua própria wallet_code.

E confirma que a verificação de assinatura do webhook bate com o exemplo
Node.js publicado na documentação: HMAC-SHA256 em hex, sobre o corpo cru.
"""

import hashlib
import hmac
import json
from decimal import Decimal
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings

from .providers.base import FAILED, PENDING, SUCCEEDED, InvalidSignature, PaymentError
from .providers.debitopay import DebitoPayProvider

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


def _resp(payload: dict, ok: bool = True) -> Mock:
    m = Mock()
    m.ok = ok
    m.status_code = 200 if ok else 400
    m.json.return_value = payload
    return m


@override_settings(DEBITOPAY=DEBITOPAY_TEST)
class CreateChargeTests(TestCase):
    def setUp(self):
        self.provider = DebitoPayProvider()

    @patch("payments.providers.debitopay.requests.post")
    def test_mpesa_envia_a_wallet_e_o_telefone_certos(self, post):
        post.return_value = _resp({
            "success": True, "payment_id": "pay_1", "payment_method": "mpesa",
            "status": "success", "transactionId": "DD55JOL0XYT", "reference": "DD55JOL0XYT",
        })
        self.provider.create_charge(
            amount=Decimal("150"), currency="MZN", reference="TCKT1",
            phone="258841234567", method="mpesa", description="teste",
            callback_url="https://x/cb",
        )
        sent = post.call_args.kwargs["json"]
        self.assertEqual(sent["payment_method"], "mpesa")
        self.assertEqual(sent["wallet_code"], "12345")            # a carteira do mpesa
        self.assertEqual(sent["merchant_id"], DEBITOPAY_TEST["MERCHANT_ID"])
        self.assertEqual(sent["phone"], "258841234567")
        self.assertEqual(sent["amount"], 150.0)
        self.assertNotIn("return_url", sent)                       # só cartões usam isto

    @patch("payments.providers.debitopay.requests.post")
    def test_mpesa_confirma_de_forma_sincrona(self, post):
        """A particularidade principal deste gateway: não há pending → webhook
        para M-Pesa. O status já vem 'success' na primeira resposta."""
        post.return_value = _resp({
            "success": True, "payment_id": "pay_1", "payment_method": "mpesa",
            "status": "success", "reference": "DD55JOL0XYT",
        })
        charge = self.provider.create_charge(
            amount=Decimal("150"), currency="MZN", reference="TCKT1",
            phone="258841234567", method="mpesa", description="", callback_url="",
        )
        self.assertEqual(charge.status, SUCCEEDED)
        self.assertEqual(charge.reference, "pay_1")

    @patch("payments.providers.debitopay.requests.post")
    def test_emola_fica_pendente_ate_ao_callback(self, post):
        post.return_value = _resp({
            "success": True, "payment_id": "pay_2", "payment_method": "emola",
            "status": "pending", "reference": "EH2026...", "awaiting_confirmation": True,
        })
        charge = self.provider.create_charge(
            amount=Decimal("750"), currency="MZN", reference="TCKT2",
            phone="258861234567", method="emola", description="", callback_url="",
        )
        self.assertEqual(charge.status, PENDING)

    @patch("payments.providers.debitopay.requests.post")
    def test_cartao_usa_return_url_e_devolve_checkout_url(self, post):
        post.return_value = _resp({
            "success": True, "payment_id": "pay_3", "payment_method": "visa_mastercard",
            "status": "pending",
            "checkout_url": "https://debitopay.com/checkout/card?session_id=abc",
        })
        charge = self.provider.create_charge(
            amount=Decimal("500"), currency="MZN", reference="TCKT3",
            phone="", method="card", description="Evento X", callback_url="https://loja/result",
        )
        sent = post.call_args.kwargs["json"]
        self.assertEqual(sent["payment_method"], "visa_mastercard")   # sinónimo resolvido
        self.assertEqual(sent["return_url"], "https://loja/result")
        self.assertNotIn("phone", sent)
        self.assertTrue(charge.checkout_url.startswith("https://debitopay.com"))

    @patch("payments.providers.debitopay.requests.post")
    def test_erro_do_gateway_vira_paymenterror_legivel(self, post):
        post.return_value = _resp({"success": False, "error": "INVALID_API_KEY"}, ok=False)
        with self.assertRaises(PaymentError) as ctx:
            self.provider.create_charge(
                amount=Decimal("10"), currency="MZN", reference="TCKT4",
                phone="258840000000", method="mpesa", description="", callback_url="",
            )
        self.assertIn("INVALID_API_KEY", str(ctx.exception))

    def test_metodo_sem_wallet_configurada_falha_cedo(self):
        """Não deixa chegar à rede sem saber para que carteira enviar."""
        provider = DebitoPayProvider()
        provider.wallets = {**provider.wallets, "payfast": ""}
        with self.assertRaises(PaymentError):
            provider._method_for("payfast")


@override_settings(DEBITOPAY=DEBITOPAY_TEST)
class CheckStatusTests(TestCase):
    @patch("payments.providers.debitopay.requests.post")
    def test_fetch_charge_usa_a_action_check_status(self, post):
        post.return_value = _resp({
            "success": True,
            "payment": {"id": "pay_1", "status": "success", "payment_method": "mpesa",
                       "amount": 150, "currency": "MZN"},
        })
        charge = DebitoPayProvider().fetch_charge("pay_1")
        sent = post.call_args.kwargs["json"]
        self.assertEqual(sent["action"], "check-status")
        self.assertEqual(sent["payment_id"], "pay_1")
        self.assertEqual(charge.status, SUCCEEDED)


@override_settings(DEBITOPAY=DEBITOPAY_TEST)
class WebhookTests(TestCase):
    """A assinatura tem de bater com o exemplo Node.js da documentação:
    HMAC-SHA256 em hex, sobre os bytes crus do corpo."""

    def setUp(self):
        self.provider = DebitoPayProvider()
        self.payload = {
            "event": "payment.completed",
            "data": {
                "payment_id": "pay_1", "merchant_id": DEBITOPAY_TEST["MERCHANT_ID"],
                "wallet_code": "12345", "amount": 150, "currency": "MZN",
                "method": "mpesa", "reference": "DD55JOL0XYT",
                "paid_at": "2026-04-18T12:02:15Z",
            },
            "timestamp": "2026-04-18T12:02:16Z",
        }
        self.body = json.dumps(self.payload).encode()

    def _sign(self, body: bytes) -> str:
        return hmac.new(
            DEBITOPAY_TEST["WEBHOOK_SECRET"].encode(), body, hashlib.sha256
        ).hexdigest()

    def test_assinatura_valida_e_aceite(self):
        event = self.provider.parse_webhook(
            self.body, {"X-Webhook-Signature": self._sign(self.body)}
        )
        self.assertEqual(event.status, SUCCEEDED)
        self.assertEqual(event.charge_reference, "pay_1")
        self.assertEqual(event.amount, Decimal("150"))

    def test_assinatura_invalida_e_recusada(self):
        with self.assertRaises(InvalidSignature):
            self.provider.parse_webhook(self.body, {"X-Webhook-Signature": "errada"})

    def test_payment_failed_mapeia_para_failed(self):
        payload = {**self.payload, "event": "payment.failed"}
        body = json.dumps(payload).encode()
        event = self.provider.parse_webhook(body, {"X-Webhook-Signature": self._sign(body)})
        self.assertEqual(event.status, FAILED)

    def test_assinatura_e_sobre_o_corpo_cru_nao_sobre_o_dict_reserializado(self):
        """Se alguém reserializar o JSON antes de assinar, a validação tem de
        falhar — é o erro mais comum de integrar HMAC de webhooks."""
        assinatura_de_outro_corpo = self._sign(json.dumps(self.payload, indent=2).encode())
        with self.assertRaises(InvalidSignature):
            self.provider.parse_webhook(
                self.body, {"X-Webhook-Signature": assinatura_de_outro_corpo}
            )


@override_settings(DEBITOPAY=DEBITOPAY_TEST, PAYMENT_PROVIDER="debitopay")
class StartPaymentIntegrationTests(TestCase):
    """Prova que start_payment() lida com a confirmação síncrona do M-Pesa —
    o bilhete tem de sair pago, sem esperar por nenhum webhook."""

    def setUp(self):
        from datetime import timedelta
        from decimal import Decimal as D
        from django.utils import timezone
        from catalog.models import Event, Price
        from partners.models import User
        from payments.providers.registry import reset

        reset()   # limpa a cache do registry entre testes com settings diferentes
        self.org = User.objects.create_user(
            "org_dp", email="org_dp@test.local", password="Pa$$w0rd!123"
        )
        self.event = Event.objects.create(
            organizer=self.org, name="Evento", date=timezone.now() + timedelta(days=10),
            status=Event.Status.PUBLISHED,
        )
        self.price = Price.objects.create(
            event=self.event, name="Geral", amount=D("150.00"), quantity_total=5
        )

    def tearDown(self):
        from payments.providers.registry import reset
        reset()

    @patch("payments.providers.debitopay.requests.post")
    def test_bilhete_mpesa_sai_pago_sem_webhook(self, post):
        from ticketing.services import create_ticket
        from payments.services import start_payment

        post.return_value = _resp({
            "success": True, "payment_id": "pay_sync_1", "payment_method": "mpesa",
            "status": "success", "amount": 150, "currency": "MZN",
            "transactionId": "DD001", "reference": "DD001",
        })

        ticket = create_ticket(
            price_id=self.price.id, event_id=self.event.id,
            phone="258841234567", issued_to=self.org, payment_method="mpesa",
        )
        self.assertEqual(ticket.payment, "pending")   # antes de chamar o gateway

        start_payment(ticket, callback_url="https://x/cb")
        ticket.refresh_from_db()
        self.assertEqual(ticket.payment, "paid")
        self.assertEqual(ticket.provider_charge_id, "pay_sync_1")

    @patch("payments.providers.debitopay.requests.post")
    def test_bilhete_mpesa_com_valor_adulterado_nao_confirma(self, post):
        """Mesmo na confirmação síncrona, o valor devolvido tem de bater
        com o preço do bilhete — a verificação não é exclusiva do webhook."""
        from ticketing.services import create_ticket
        from payments.services import start_payment

        post.return_value = _resp({
            "success": True, "payment_id": "pay_sync_2", "payment_method": "mpesa",
            "status": "success", "amount": 1, "currency": "MZN",   # devia ser 150
        })
        ticket = create_ticket(
            price_id=self.price.id, event_id=self.event.id,
            phone="258841234567", issued_to=self.org, payment_method="mpesa",
        )
        start_payment(ticket, callback_url="https://x/cb")
        ticket.refresh_from_db()
        self.assertEqual(ticket.payment, "pending")   # retido, não confirmado
