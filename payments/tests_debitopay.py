"""Testes do adaptador Debito Pay: constrói o pedido certo e interpreta a
resposta certa, sem tocar na rede — usa unittest.mock.patch em requests.post.

Cobrem as duas particularidades deste gateway:

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

from . import debitopay
from .debitopay import FAILED, PENDING, REFUNDED, SUCCEEDED
from .exceptions import InvalidSignature, PaymentError

DEBITOPAY_TEST = {
    "BASE_URL": "https://gyqoaningqhurhvdugne.supabase.co/functions/v1",
    "SECRET_KEY": "sk_live_teste",
    "WEBHOOK_SECRET": "webhook-secret-live-teste",
    "SIGNATURE_HEADER": "X-Webhook-Signature",
    "MERCHANT_ID": "11111111-1111-1111-1111-111111111111",
    "WALLETS": {
        "mpesa": "12345", "emola": "22222", "mkesh": "33333",
        "visa_mastercard": "44444", "payfast": "55555",
    },
    "DEFAULT_METHOD": "mpesa",
    "TIMEOUT": 30,
}

DEBITOPAY_SANDBOX_TEST = {
    "BASE_URL": "https://sandbox.example/functions/v1",
    "SECRET_KEY": "sk_sandbox_teste",
    "WEBHOOK_SECRET": "webhook-secret-sandbox-teste",
    "SIGNATURE_HEADER": "X-Webhook-Signature",
    "MERCHANT_ID": "99999999-9999-9999-9999-999999999999",
    "WALLETS": {
        "mpesa": "s-12345", "emola": "s-22222", "mkesh": "s-33333",
        "visa_mastercard": "s-44444", "payfast": "s-55555",
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
    @patch("payments.debitopay.requests.post")
    def test_mpesa_envia_a_wallet_e_o_telefone_certos(self, post):
        post.return_value = _resp({
            "success": True, "payment_id": "pay_1", "payment_method": "mpesa",
            "status": "success", "transactionId": "DD55JOL0XYT", "reference": "DD55JOL0XYT",
        })
        debitopay.create_charge(
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

    @patch("payments.debitopay.requests.post")
    def test_mpesa_confirma_de_forma_sincrona(self, post):
        """A particularidade principal deste gateway: não há pending → webhook
        para M-Pesa. O status já vem 'success' na primeira resposta."""
        post.return_value = _resp({
            "success": True, "payment_id": "pay_1", "payment_method": "mpesa",
            "status": "success", "reference": "DD55JOL0XYT",
        })
        charge = debitopay.create_charge(
            amount=Decimal("150"), currency="MZN", reference="TCKT1",
            phone="258841234567", method="mpesa", description="", callback_url="",
        )
        self.assertEqual(charge.status, SUCCEEDED)
        self.assertEqual(charge.reference, "pay_1")

    @patch("payments.debitopay.requests.post")
    def test_emola_fica_pendente_ate_ao_callback(self, post):
        post.return_value = _resp({
            "success": True, "payment_id": "pay_2", "payment_method": "emola",
            "status": "pending", "reference": "EH2026...", "awaiting_confirmation": True,
        })
        charge = debitopay.create_charge(
            amount=Decimal("750"), currency="MZN", reference="TCKT2",
            phone="258861234567", method="emola", description="", callback_url="",
        )
        self.assertEqual(charge.status, PENDING)

    @patch("payments.debitopay.requests.post")
    def test_cartao_usa_return_url_e_devolve_checkout_url(self, post):
        post.return_value = _resp({
            "success": True, "payment_id": "pay_3", "payment_method": "visa_mastercard",
            "status": "pending",
            "checkout_url": "https://debitopay.com/checkout/card?session_id=abc",
        })
        charge = debitopay.create_charge(
            amount=Decimal("500"), currency="MZN", reference="TCKT3",
            phone="", method="card", description="Evento X", callback_url="https://loja/result",
        )
        sent = post.call_args.kwargs["json"]
        self.assertEqual(sent["payment_method"], "visa_mastercard")   # sinónimo resolvido
        self.assertEqual(sent["return_url"], "https://loja/result")
        self.assertNotIn("phone", sent)
        self.assertTrue(charge.checkout_url.startswith("https://debitopay.com"))

    @patch("payments.debitopay.requests.post")
    def test_erro_do_gateway_vira_paymenterror_legivel(self, post):
        post.return_value = _resp({"success": False, "error": "INVALID_API_KEY"}, ok=False)
        with self.assertRaises(PaymentError) as ctx:
            debitopay.create_charge(
                amount=Decimal("10"), currency="MZN", reference="TCKT4",
                phone="258840000000", method="mpesa", description="", callback_url="",
            )
        self.assertIn("INVALID_API_KEY", str(ctx.exception))

    def test_metodo_sem_wallet_configurada_falha_cedo(self):
        """Não deixa chegar à rede sem saber para que carteira enviar."""
        with override_settings(DEBITOPAY={**DEBITOPAY_TEST,
                                          "WALLETS": {**DEBITOPAY_TEST["WALLETS"], "payfast": ""}}):
            with self.assertRaises(PaymentError):
                debitopay._method_for("payfast")


@override_settings(DEBITOPAY=DEBITOPAY_TEST, DEBITOPAY_SANDBOX=DEBITOPAY_SANDBOX_TEST)
class SandboxRoutingTests(TestCase):
    """Fase 4.2: um bilhete criado com uma chave etk_test_… não pode cobrar
    de verdade — sandbox=True troca as credenciais e a wallet, sem tocar
    no resto da lógica."""

    @patch("payments.debitopay.requests.post")
    def test_sandbox_usa_a_url_e_as_credenciais_da_sandbox(self, post):
        post.return_value = _resp({
            "success": True, "payment_id": "pay_sandbox_1", "payment_method": "mpesa",
            "status": "pending",
        })
        debitopay.create_charge(
            amount=Decimal("150"), currency="MZN", reference="TCKT1",
            phone="258841234567", method="mpesa", description="", callback_url="",
            sandbox=True,
        )
        called_url = post.call_args.args[0] if post.call_args.args else post.call_args.kwargs["url"]
        self.assertTrue(called_url.startswith(DEBITOPAY_SANDBOX_TEST["BASE_URL"]))
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"],
                         f"Bearer {DEBITOPAY_SANDBOX_TEST['SECRET_KEY']}")
        sent = post.call_args.kwargs["json"]
        self.assertEqual(sent["wallet_code"], DEBITOPAY_SANDBOX_TEST["WALLETS"]["mpesa"])
        self.assertEqual(sent["merchant_id"], DEBITOPAY_SANDBOX_TEST["MERCHANT_ID"])

    @patch("payments.debitopay.requests.post")
    def test_sem_sandbox_continua_a_usar_a_conta_live(self, post):
        post.return_value = _resp({
            "success": True, "payment_id": "pay_live_1", "payment_method": "mpesa",
            "status": "pending",
        })
        debitopay.create_charge(
            amount=Decimal("150"), currency="MZN", reference="TCKT1",
            phone="258841234567", method="mpesa", description="", callback_url="",
        )
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"],
                         f"Bearer {DEBITOPAY_TEST['SECRET_KEY']}")

    def test_sandbox_sem_wallet_falha_com_a_mensagem_certa(self):
        vazio = {**DEBITOPAY_SANDBOX_TEST,
                "WALLETS": {**DEBITOPAY_SANDBOX_TEST["WALLETS"], "payfast": ""}}
        with override_settings(DEBITOPAY_SANDBOX=vazio):
            with self.assertRaises(PaymentError) as ctx:
                debitopay._method_for("payfast", sandbox=True)
            self.assertIn("DEBITOPAY_SANDBOX_WALLET_PAYFAST", str(ctx.exception))


@override_settings(DEBITOPAY=DEBITOPAY_TEST)
class CheckStatusTests(TestCase):
    @patch("payments.debitopay.requests.post")
    def test_fetch_charge_usa_a_action_check_status(self, post):
        post.return_value = _resp({
            "success": True,
            "payment": {"id": "pay_1", "status": "success", "payment_method": "mpesa",
                       "amount": 150, "currency": "MZN"},
        })
        charge = debitopay.fetch_charge("pay_1")
        sent = post.call_args.kwargs["json"]
        self.assertEqual(sent["action"], "check-status")
        self.assertEqual(sent["payment_id"], "pay_1")
        self.assertEqual(charge.status, SUCCEEDED)


@override_settings(DEBITOPAY=DEBITOPAY_TEST, DEBITOPAY_SANDBOX=DEBITOPAY_SANDBOX_TEST)
class WebhookTests(TestCase):
    """A assinatura tem de bater com o exemplo Node.js da documentação:
    HMAC-SHA256 em hex, sobre os bytes crus do corpo."""

    def setUp(self):
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

    def _headers(self, body: bytes) -> dict:
        return {"X-Webhook-Signature": self._sign(body)}

    def test_assinatura_valida_e_aceite(self):
        event = debitopay.parse_webhook(self.body, self._headers(self.body))
        self.assertEqual(event.status, SUCCEEDED)
        self.assertEqual(event.charge_reference, "pay_1")
        self.assertEqual(event.amount, Decimal("150"))

    def test_assinatura_invalida_e_recusada(self):
        with self.assertRaises(InvalidSignature):
            debitopay.parse_webhook(self.body, {"X-Webhook-Signature": "errada"})

    def test_assinatura_da_sandbox_tambem_e_aceite(self):
        """Fase 4.2: o mesmo endpoint recebe webhooks das duas contas — um
        bilhete de teste tem o seu evento assinado com o segredo sandbox,
        não o live."""
        sig = hmac.new(
            DEBITOPAY_SANDBOX_TEST["WEBHOOK_SECRET"].encode(), self.body, hashlib.sha256
        ).hexdigest()
        event = debitopay.parse_webhook(self.body, {"X-Webhook-Signature": sig})
        self.assertEqual(event.charge_reference, "pay_1")

    def test_payment_failed_mapeia_para_failed(self):
        payload = {**self.payload, "event": "payment.failed"}
        body = json.dumps(payload).encode()
        event = debitopay.parse_webhook(body, self._headers(body))
        self.assertEqual(event.status, FAILED)

    def test_payment_refunded_mapeia_para_refunded(self):
        payload = {**self.payload, "event": "payment.refunded"}
        body = json.dumps(payload).encode()
        event = debitopay.parse_webhook(body, self._headers(body))
        self.assertEqual(event.status, REFUNDED)

    def test_payment_chargeback_mapeia_para_refunded(self):
        """Chargeback e reembolso têm o mesmo efeito no bilhete: anulá-lo."""
        payload = {**self.payload, "event": "payment.chargeback"}
        body = json.dumps(payload).encode()
        event = debitopay.parse_webhook(body, self._headers(body))
        self.assertEqual(event.status, REFUNDED)

    def test_assinatura_e_sobre_o_corpo_cru_nao_sobre_o_dict_reserializado(self):
        """Se alguém reserializar o JSON antes de assinar, a validação tem de
        falhar — é o erro mais comum de integrar HMAC de webhooks."""
        assinatura_de_outro_corpo = self._sign(json.dumps(self.payload, indent=2).encode())
        with self.assertRaises(InvalidSignature):
            debitopay.parse_webhook(
                self.body, {"X-Webhook-Signature": assinatura_de_outro_corpo}
            )
