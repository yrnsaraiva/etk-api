"""Testes do pagamento com a iMali.Way: da compra ao webhook, passando pela
reconciliação, e o adaptador isolado. Nunca tocam na rede — simulam o gateway
com unittest.mock.patch em `requests.request`."""

import base64
import hashlib
import hmac
import json
import time
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from catalog.models import Event, Price
from partners.models import ApiKey, User
from payments import imaliway
from payments.exceptions import InvalidSignature, PaymentDeclined, PaymentError, ProviderUnavailable
from payments.models import ProviderEvent
from payments.services import reconcile_pending
from ticketing.models import Ticket
from ticketing.services import confirm_payment, release

WEBHOOK = "/back/payments/webhooks/imaliway"

# Par RSA de teste: o adaptador cifra com a pública; os testes decifram com a
# privada para provar que a privateKey leva mesmo a api_key lá dentro.
_RSA = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PUBLIC_PEM = _RSA.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()

IMALIWAY_TEST = {
    "BASE_URL": "https://gateway.test/api/partners/imaliway/v2",
    "API_KEY": "api-key-live-teste",
    "CLIENT_ID": "1111111111",
    "PUBLIC_KEY": _PUBLIC_PEM,
    "STORE_ACCOUNT_NUMBER": "260000079",
    "WEBHOOK_SECRET": "whs_live_teste",
    "DEFAULT_METHOD": "mpesa",
    "PUSH_EXPIRATION_MINUTES": 0,
    "TIMEOUT": 30,
}
IMALIWAY_SANDBOX_TEST = {
    "BASE_URL": "https://sandbox.test/api/partners/imaliway/v2",
    "API_KEY": "api-key-sandbox-teste",
    "CLIENT_ID": "2222222222",
    "PUBLIC_KEY": _PUBLIC_PEM,
    "STORE_ACCOUNT_NUMBER": "290000001",
    "WEBHOOK_SECRET": "whs_sandbox_teste",
    "DEFAULT_METHOD": "mpesa",
    "PUSH_EXPIRATION_MINUTES": 0,
    "TIMEOUT": 30,
}


def _resp(payload: dict, status: int = 200) -> Mock:
    m = Mock()
    m.ok = 200 <= status < 300
    m.status_code = status
    m.json.return_value = payload
    return m


def _created(method, url, **kwargs):
    """Resposta da iMali.Way a um push: ecoa o partner_transaction_id enviado."""
    sent = kwargs["json"]
    return _resp({"data": {
        "transaction_id": "EML26D7ALOJ7", "partner_transaction_id": sent["partner_transaction_id"],
        "amount": str(sent["amount"]), "expiration_datetime": sent.get("expiration_datetime", "2026-10-20 11:02:51"),
        "status": "PENDING"}})


def _status(status: str, reference: str, **extra) -> Mock:
    return _resp({"data": {"status": status, "left_time": "1:45", **extra}})


def _sign(body: bytes, timestamp: str | None, secret: str = IMALIWAY_TEST["WEBHOOK_SECRET"],
          prefix: bool = True) -> str:
    signed = f"{timestamp}.".encode() + body if timestamp else body
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"sha256={digest}" if prefix else digest


def _webhook_body(event_type: str, reference: str, amount="300.00", status=None,
                  payment_type="C2B") -> bytes:
    status = status or event_type.split(".")[-1]
    return json.dumps({
        "id": f"evt_{reference}_{event_type}", "type": event_type, "payment_type": payment_type,
        "data": {"amount": amount, "status": status, "payment_type": payment_type,
                 "transaction_id": "EML26D7ALOJ7", "partner_transaction_id": reference,
                 "expiration_datetime": "2026-10-20 11:02:51"},
    }).encode()


@override_settings(IMALIWAY=IMALIWAY_TEST, IMALIWAY_SANDBOX=IMALIWAY_SANDBOX_TEST)
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

    @patch("payments.imaliway.requests.request", side_effect=_created)
    def comprar(self, request, phone="258841111111", method="emola"):
        """Compra um bilhete com o push a ficar PENDING, para os testes de
        webhook controlarem o desfecho."""
        r = self.api.post("/back/borrow/external/tickets",
                          {"priceId": self.price.id, "eventId": self.event.id,
                           "phone": phone, "paymentMethod": method}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.last_request = request.call_args
        return Ticket.objects.get(pk=r.data["data"]["id"])

    def enviar(self, body: bytes, *, timestamp="now", prefix=True, secret=IMALIWAY_TEST["WEBHOOK_SECRET"],
               ts_header="HTTP_X_WEBHOOK_TIMESTAMP", sig_header="HTTP_X_WEBHOOK_SIGNATURE"):
        ts = str(int(time.time())) if timestamp == "now" else timestamp
        extra = {sig_header: _sign(body, ts, secret, prefix)}
        if ts:
            extra[ts_header] = ts
        return self.client.post(WEBHOOK, data=body, content_type="application/json", **extra)


class CobrancaTests(Base):
    def test_compra_abre_push_no_gateway(self):
        t = self.comprar()
        self.assertEqual(t.provider, "imaliway")
        self.assertEqual(t.provider_charge_id, imaliway.partner_transaction_id(t.id))
        self.assertRegex(t.provider_charge_id, r"^[A-Z2-7]{12}$")          # 12 alfanuméricos
        self.assertEqual(t.payment, Ticket.Payment.PENDING)
        self.assertIsNotNone(t.expires_at)

    def test_pedido_segue_o_contrato(self):
        t = self.comprar(phone="258841111111", method="emola")
        args, kwargs = self.last_request
        self.assertEqual(args, ("POST", "https://gateway.test/api/partners/imaliway/v2/payments"))
        sent = kwargs["json"]
        self.assertEqual(sent["client_account_number"], "841111111")       # 9 dígitos, sem 258
        self.assertEqual(sent["amount"], 300)
        self.assertIsInstance(sent["amount"], int)                         # 300 e não 300.0
        self.assertEqual(sent["store_account_number"], "260000079")
        self.assertEqual(sent["partner_transaction_id"], t.provider_charge_id)
        self.assertEqual(sent["payment_method"], "emola")
        self.assertEqual((sent["payment_type"], sent["transaction_type"]), ("push", "C2B"))
        # como no exemplo de teste da Paytek: sem expiration_datetime, vale o padrão (2 min)
        self.assertNotIn("expiration_datetime", sent)
        # nem chegar a ligar deve falhar depressa: (ligar, ler)
        self.assertEqual(self.last_request.kwargs["timeout"], (10, 30))

    def test_authorization_leva_a_api_key_cifrada_com_a_chave_publica(self):
        self.comprar()
        headers = self.last_request.kwargs["headers"]
        self.assertEqual(headers["X-Client-ID"], "1111111111")
        token = headers["Authorization"].removeprefix("Bearer ")
        decrypted = _RSA.decrypt(base64.b64decode(token), padding.PKCS1v15())
        self.assertEqual(decrypted, b"api-key-live-teste")

    def test_a_cifra_e_aleatoria_mas_sempre_valida(self):
        with override_settings(IMALIWAY=IMALIWAY_TEST):
            a, b = imaliway.private_key(), imaliway.private_key()
        self.assertNotEqual(a, b)
        for token in (a, b):
            self.assertEqual(_RSA.decrypt(base64.b64decode(token), padding.PKCS1v15()),
                             b"api-key-live-teste")

    @override_settings(IMALIWAY={**IMALIWAY_TEST, "PUSH_EXPIRATION_MINUTES": 5})
    def test_expiracao_opcional_nunca_passa_da_reserva(self):
        t = self.comprar()
        from datetime import datetime
        from zoneinfo import ZoneInfo
        sent = self.last_request.kwargs["json"]["expiration_datetime"]
        self.assertRegex(sent, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        limit = datetime.strptime(sent, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("Africa/Maputo"))
        self.assertLessEqual(limit, t.expires_at + timedelta(seconds=1))
        self.assertLessEqual(limit, timezone.now() + timedelta(minutes=5, seconds=2))


class RecusaTests(Base):
    def test_valor_abaixo_do_minimo_nao_chama_o_gateway_e_liberta_a_vaga(self):
        self.price.amount = Decimal("5.00")
        self.price.save()
        with patch("payments.imaliway.requests.request") as net:
            r = self.api.post("/back/borrow/external/tickets",
                              {"priceId": self.price.id, "eventId": self.event.id,
                               "phone": "258841111111", "paymentMethod": "mpesa"}, format="json")
        self.assertEqual(r.status_code, 402)
        net.assert_not_called()
        self.assertEqual(Ticket.objects.get().payment, Ticket.Payment.FAILED)
        self.assertEqual(Price.objects.get(pk=self.price.pk).available, 5)

    def test_cartao_nao_esta_disponivel(self):
        with patch("payments.imaliway.requests.request") as net:
            r = self.api.post("/back/borrow/external/tickets",
                              {"priceId": self.price.id, "eventId": self.event.id,
                               "phone": "258841111111", "paymentMethod": "card"}, format="json")
        self.assertEqual(r.status_code, 402)
        net.assert_not_called()                      # nunca cai em M-Pesa por engano

    def test_recusa_do_gateway_da_402_e_liberta_a_vaga(self):
        recusa = _resp({"message_cod": 407, "message": "Insufficient funds"}, status=407)
        with patch("payments.imaliway.requests.request", return_value=recusa):
            r = self.api.post("/back/borrow/external/tickets",
                              {"priceId": self.price.id, "eventId": self.event.id,
                               "phone": "258841111111", "paymentMethod": "mpesa"}, format="json")
        self.assertEqual(r.status_code, 402)
        self.assertEqual(Ticket.objects.get().payment, Ticket.Payment.FAILED)
        self.assertEqual(Price.objects.get(pk=self.price.pk).available, 5)

    def test_gateway_em_baixo_da_502_e_liberta_a_vaga(self):
        with patch("payments.imaliway.requests.request", side_effect=requests.ConnectionError("x")):
            r = self.api.post("/back/borrow/external/tickets",
                              {"priceId": self.price.id, "eventId": self.event.id,
                               "phone": "258841111111", "paymentMethod": "mpesa"}, format="json")
        self.assertEqual(r.status_code, 502)
        self.assertEqual(Ticket.objects.get().payment, Ticket.Payment.FAILED)


@override_settings(IMALIWAY=IMALIWAY_TEST, IMALIWAY_SANDBOX=IMALIWAY_SANDBOX_TEST)
class AdaptadorTests(SimpleTestCase):
    """O adaptador isolado."""

    def charge(self, **over):
        kw = dict(amount=Decimal("300"), currency="MZN", reference="TCKT17907699473511",
                  phone="258841111111", method="mpesa")
        kw.update(over)
        return imaliway.create_charge(**kw)

    def test_numero_local_e_validado(self):
        self.assertEqual(imaliway._local_number("258841111111"), "841111111")
        self.assertEqual(imaliway._local_number("84 111 1111"), "841111111")
        for bad in ("", "12345", "2588411111"):
            with self.assertRaises(PaymentError):
                imaliway._local_number(bad)

    def test_partner_transaction_id_e_estavel_e_unico(self):
        a = imaliway.partner_transaction_id("TCKT17907699473511")
        self.assertEqual(a, imaliway.partner_transaction_id("TCKT17907699473511"))
        self.assertNotEqual(a, imaliway.partner_transaction_id("TCKT17907699473512"))
        self.assertEqual(len(a), 12)

    def test_metodos(self):
        self.assertEqual(imaliway._method_for("M-Pesa"), "mpesa")
        self.assertEqual(imaliway._method_for("e-Mola"), "emola")
        self.assertEqual(imaliway._method_for(""), "mpesa")                # por omissão
        for bad in ("card", "visa", "bitcoin"):
            with self.assertRaises(PaymentDeclined):
                imaliway._method_for(bad)

    def test_valor_minimo_e_moeda(self):
        with patch("payments.imaliway.requests.request") as net:
            with self.assertRaises(PaymentDeclined):
                self.charge(amount=Decimal("9.99"))
            with self.assertRaises(PaymentDeclined):
                self.charge(currency="USD")
        net.assert_not_called()

    def test_repetir_o_pedido_nao_duplica_a_cobranca(self):
        """406 (partner_transaction_id em uso) = esta cobrança já existe: devolve o estado."""
        em_uso = _resp({"message_cod": 406, "message": "in use"}, status=406)
        with patch("payments.imaliway.requests.request",
                   side_effect=[em_uso, _status("PENDING", "x")]) as net:
            charge = self.charge()
        self.assertEqual(charge.status, imaliway.PENDING)
        self.assertEqual(charge.reference, imaliway.partner_transaction_id("TCKT17907699473511"))
        self.assertEqual(net.call_args.args[0], "GET")                      # foi consultar

    def test_codigos_de_erro(self):
        with patch("payments.imaliway.requests.request",
                   return_value=_resp({"message_cod": 502, "message": "Store KYC"}, status=200)):
            with self.assertRaises(PaymentDeclined) as ctx:
                self.charge()
        self.assertEqual(ctx.exception.code, "502")
        # 500 = token inválido: é configuração, não uma recusa ao cliente
        with patch("payments.imaliway.requests.request",
                   return_value=_resp({"message_cod": 500, "message": "Invalid token"}, status=500)):
            with self.assertRaises(PaymentError) as ctx:
                self.charge()
        self.assertNotIsInstance(ctx.exception, PaymentDeclined)
        # servidor em baixo, sem corpo útil: transporte, repetível
        with patch("payments.imaliway.requests.request", return_value=_resp({}, status=503)):
            with self.assertRaises(ProviderUnavailable):
                self.charge()
        # erros de validação ({"errors": {...}}) trazem a mensagem
        with patch("payments.imaliway.requests.request", return_value=_resp(
                {"errors": {"payment_method": ["Metodos de pagamentos validos:imali,mpesa,emola,mkesh"]}},
                status=422)):
            with self.assertRaises(PaymentDeclined) as ctx:
                self.charge()
        self.assertIn("payment_method", str(ctx.exception))

    def test_valor_json_inteiro_ou_decimal(self):
        self.assertEqual(imaliway._json_amount(Decimal("10")), 10)
        self.assertIsInstance(imaliway._json_amount(Decimal("10.00")), int)
        self.assertEqual(imaliway._json_amount(Decimal("12.5")), 12.5)
        self.assertEqual(imaliway._json_amount(Decimal("299.99")), 299.99)

    def test_sem_ligar_a_mensagem_aponta_para_a_whitelist_de_ip(self):
        for exc in (requests.ConnectTimeout("t"), requests.ConnectionError("c")):
            with patch("payments.imaliway.requests.request", side_effect=exc):
                with self.assertRaises(ProviderUnavailable) as ctx:
                    self.charge()
            self.assertIn("whitelist", str(ctx.exception))
            self.assertIn("check_imaliway", str(ctx.exception))
        with patch("payments.imaliway.requests.request", side_effect=requests.ReadTimeout("r")):
            with self.assertRaises(ProviderUnavailable) as ctx:
                self.charge()
        self.assertNotIn("whitelist", str(ctx.exception))      # lido mas lento: não é problema de IP

    def test_resposta_ilegivel_e_falha_de_transporte(self):
        bad = Mock(ok=True, status_code=200)
        bad.json.side_effect = ValueError("html")
        with patch("payments.imaliway.requests.request", return_value=bad):
            with self.assertRaises(ProviderUnavailable):
                self.charge()

    @override_settings(IMALIWAY={**IMALIWAY_TEST, "API_KEY": ""})
    def test_sem_configuracao_diz_o_que_falta(self):
        with self.assertRaises(PaymentError) as ctx:
            self.charge()
        self.assertIn("IMALIWAY_API_KEY", str(ctx.exception))

    @override_settings(IMALIWAY={**IMALIWAY_TEST, "PUBLIC_KEY": _PUBLIC_PEM.replace("\n", "\\n")})
    def test_chave_publica_com_barras_n_literais(self):
        """Em variáveis de ambiente o PEM costuma vir numa só linha."""
        self.assertTrue(imaliway.private_key())

    def test_consulta_de_estado(self):
        with patch("payments.imaliway.requests.request", return_value=_status("EXPIRED", "REF")) as net:
            charge = imaliway.fetch_charge("REFREFREFREF")
        self.assertEqual(net.call_args.args[0], "GET")
        query = {"payment_type": "push", "partner_transaction_id": "REFREFREFREF"}
        self.assertEqual(net.call_args.kwargs["params"], query)
        # o exemplo de teste da Paytek manda-os no corpo JSON do GET: vão nos dois sítios
        self.assertEqual(net.call_args.kwargs["json"], query)
        self.assertEqual(charge.status, imaliway.FAILED)                     # expirou = não pagou
        self.assertIsNone(charge.amount)                                     # a consulta não devolve o valor
        for raw, expected in (("SUCCESS", "succeeded"), ("PENDING", "pending"), ("FAILED", "failed")):
            with patch("payments.imaliway.requests.request", return_value=_status(raw, "R")):
                self.assertEqual(imaliway.fetch_charge("REFREFREFREF").status, expected)

    def test_webhook_assinaturas_aceites(self):
        body = _webhook_body("PAYMENT.SUCCESS", "REFREFREFREF")
        ts = str(int(time.time()))
        good = _sign(body, ts)
        for headers in (
            {"X-Webhook-Signature": good, "X-Webhook-Timestamp": ts},                       # exemplos
            {"X-Signature": good, "X-Timestamp": ts},                                       # tabela
            {"X-Webhook-Signature": _sign(body, ts, prefix=False), "X-Webhook-Timestamp": ts},
            {"X-Webhook-Signature": _sign(body, None)},                                     # sem timestamp
        ):
            event = imaliway.parse_webhook(body, headers)
            self.assertEqual(event.status, imaliway.SUCCEEDED, headers)
            self.assertEqual(event.charge_reference, "REFREFREFREF")
            self.assertEqual(event.amount, Decimal("300.00"))

    def test_webhook_recusa(self):
        body = _webhook_body("PAYMENT.SUCCESS", "REFREFREFREF")
        ts = str(int(time.time()))
        for headers in (
            {},                                                                             # sem assinatura
            {"X-Webhook-Signature": "sha256=falsa", "X-Webhook-Timestamp": ts},
            {"X-Webhook-Signature": _sign(body, ts, secret="outro-segredo"), "X-Webhook-Timestamp": ts},
            {"X-Webhook-Signature": _sign(body, "1"), "X-Webhook-Timestamp": ts},           # assinado com outro ts
        ):
            with self.assertRaises(InvalidSignature, msg=headers):
                imaliway.parse_webhook(body, headers)

    def test_webhook_fora_da_janela_anti_replay(self):
        body = _webhook_body("PAYMENT.SUCCESS", "REFREFREFREF")
        old = str(int(time.time()) - 301)
        with self.assertRaises(InvalidSignature) as ctx:
            imaliway.parse_webhook(body, {"X-Webhook-Signature": _sign(body, old),
                                          "X-Webhook-Timestamp": old})
        self.assertIn("expirado", str(ctx.exception))

    def test_webhook_aceita_o_segredo_da_sandbox(self):
        body = _webhook_body("PAYMENT.SUCCESS", "REFREFREFREF")
        ts = str(int(time.time()))
        sig = _sign(body, ts, secret=IMALIWAY_SANDBOX_TEST["WEBHOOK_SECRET"])
        self.assertEqual(imaliway.parse_webhook(
            body, {"X-Webhook-Signature": sig, "X-Webhook-Timestamp": ts}).status, imaliway.SUCCEEDED)

    def test_webhook_estados_e_valores(self):
        ts = str(int(time.time()))

        def parse(body):
            return imaliway.parse_webhook(body, {"X-Webhook-Signature": _sign(body, ts),
                                                 "X-Webhook-Timestamp": ts})
        self.assertEqual(parse(_webhook_body("PAYMENT.PENDING", "R")).status, imaliway.PENDING)
        self.assertEqual(parse(_webhook_body("PAYMENT.FAILED", "R")).status, imaliway.FAILED)
        self.assertEqual(parse(_webhook_body("PAYMENT.SUCCESS", "R", amount=10)).amount, Decimal("10"))
        # tipo desconhecido (ex.: um futuro reembolso) nunca confirma nem liberta nada
        self.assertEqual(parse(_webhook_body("PAYMENT.REFUNDED", "R", status="REFUNDED")).status,
                         imaliway.PENDING)
        # transferências B2C (saída de dinheiro) não são cobranças
        b2c = parse(_webhook_body("PAYMENT.SUCCESS", "R", payment_type="B2C"))
        self.assertEqual((b2c.status, b2c.charge_reference), (imaliway.PENDING, ""))


class WebhookTests(Base):
    def test_webhook_valido_confirma(self):
        t = self.comprar()
        body = _webhook_body("PAYMENT.SUCCESS", t.provider_charge_id, amount=str(t.amount))
        r = self.enviar(body)
        t.refresh_from_db()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(t.payment, Ticket.Payment.PAID)

    def test_assinatura_invalida_recusa_e_nao_toca_na_bd(self):
        t = self.comprar()
        body = _webhook_body("PAYMENT.SUCCESS", t.provider_charge_id)
        r = self.client.post(WEBHOOK, data=body, content_type="application/json",
                             HTTP_X_WEBHOOK_SIGNATURE="sha256=assinaturafalsa",
                             HTTP_X_WEBHOOK_TIMESTAMP=str(int(time.time())))
        t.refresh_from_db()
        self.assertEqual(r.status_code, 401)
        self.assertEqual(t.payment, Ticket.Payment.PENDING)
        self.assertEqual(ProviderEvent.objects.count(), 0)

    def test_webhook_deixa_rasto_nos_logs(self):
        t = self.comprar()
        body = _webhook_body("PAYMENT.SUCCESS", t.provider_charge_id, amount=str(t.amount))
        with self.assertLogs("payments.views", level="INFO") as logs:
            self.client.post(WEBHOOK, data=body, content_type="application/json",
                             HTTP_X_WEBHOOK_SIGNATURE="sha256=falsa",
                             HTTP_X_FORWARDED_FOR="162.220.232.252, 10.0.0.1")
            self.enviar(body)
        texto = "\n".join(logs.output)
        self.assertIn("recebido de 162.220.232.252", texto)
        self.assertIn("recusado (assinatura inválida) de 162.220.232.252", texto)
        self.assertIn("processado=True", texto)

    def test_replay_antigo_e_recusado(self):
        t = self.comprar()
        body = _webhook_body("PAYMENT.SUCCESS", t.provider_charge_id)
        r = self.enviar(body, timestamp=str(int(time.time()) - 3600))
        t.refresh_from_db()
        self.assertEqual(r.status_code, 401)
        self.assertEqual(t.payment, Ticket.Payment.PENDING)

    def test_cabecalhos_alternativos_da_tabela(self):
        t = self.comprar()
        body = _webhook_body("PAYMENT.SUCCESS", t.provider_charge_id)
        r = self.enviar(body, ts_header="HTTP_X_TIMESTAMP", sig_header="HTTP_X_SIGNATURE")
        t.refresh_from_db()
        self.assertEqual((r.status_code, t.payment), (200, Ticket.Payment.PAID))

    def test_webhook_repetido_e_ignorado(self):
        t = self.comprar()
        body = _webhook_body("PAYMENT.SUCCESS", t.provider_charge_id)
        self.enviar(body)
        self.enviar(body)
        self.assertEqual(ProviderEvent.objects.count(), 1)
        # 2 tentativas são esperadas e corretas: 1 ao abrir a cobrança
        # (start_payment, pending) + 1 ao confirmar (confirm_payment). O que
        # a idempotência impede é uma TERCEIRA, vinda do webhook repetido.
        self.assertEqual(t.attempts.count(), 2)

    def test_valor_adulterado_fica_em_revisao(self):
        t = self.comprar()
        body = _webhook_body("PAYMENT.SUCCESS", t.provider_charge_id, amount="1.00")
        r = self.enviar(body)
        t.refresh_from_db()
        self.assertEqual(t.payment, Ticket.Payment.REVIEW)
        self.assertIn("divergente", r.data["message"])

    def test_pagamento_falhado_liberta_a_vaga(self):
        t = self.comprar()
        antes = Price.objects.get(pk=self.price.pk).available
        self.enviar(_webhook_body("PAYMENT.FAILED", t.provider_charge_id))
        self.assertEqual(Price.objects.get(pk=self.price.pk).available, antes + 1)
        t.refresh_from_db()
        self.assertEqual(t.payment, Ticket.Payment.FAILED)

    def test_evento_pendente_nao_muda_nada(self):
        t = self.comprar()
        self.enviar(_webhook_body("PAYMENT.PENDING", t.provider_charge_id))
        t.refresh_from_db()
        self.assertEqual(t.payment, Ticket.Payment.PENDING)

    def test_cobranca_desconhecida_nao_rebenta(self):
        r = self.enviar(_webhook_body("PAYMENT.SUCCESS", "NAOEXISTE123"))
        self.assertEqual(r.status_code, 200)
        self.assertIn("desconhecida", r.data["message"])

    def test_transferencia_b2c_nao_mexe_em_bilhetes(self):
        t = self.comprar()
        r = self.enviar(_webhook_body("PAYMENT.SUCCESS", t.provider_charge_id, payment_type="B2C"))
        t.refresh_from_db()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(t.payment, Ticket.Payment.PENDING)

    def test_bilhete_de_outro_gateway_nao_e_confirmado(self):
        """Um bilhete antigo (Debito Pay) com a mesma referência nunca é tocado."""
        t = self.comprar()
        Ticket.objects.filter(pk=t.pk).update(provider="debitopay")
        self.enviar(_webhook_body("PAYMENT.SUCCESS", t.provider_charge_id))
        t.refresh_from_db()
        self.assertEqual(t.payment, Ticket.Payment.PENDING)

    def test_pagamento_tardio_com_vaga_confirma_sem_rebentar(self):
        """A reserva já expirou: tenta reservar de novo e confirma, nunca 500."""
        t = self.comprar()
        release(t, Ticket.Payment.FAILED)   # simula o cron de expiração
        t.refresh_from_db()
        self.assertEqual(t.payment, Ticket.Payment.FAILED)

        r = self.enviar(_webhook_body("PAYMENT.SUCCESS", t.provider_charge_id, amount=str(t.amount)))
        t.refresh_from_db()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(t.payment, Ticket.Payment.PAID)

    def test_pagamento_tardio_sem_vaga_fica_em_revisao_sem_rebentar(self):
        self.price.quantity_total = 2   # para esgotar o lote com só 2 bilhetes
        self.price.save(update_fields=["quantity_total"])

        t1 = self.comprar(phone="258841111111")
        confirm_payment(t1, provider="imaliway", provider_reference="ref1")
        t2 = self.comprar(phone="258842222222")
        release(t2, Ticket.Payment.FAILED)
        t3 = self.comprar(phone="258843333333")
        confirm_payment(t3, provider="imaliway", provider_reference="ref3")

        r = self.enviar(_webhook_body("PAYMENT.SUCCESS", t2.provider_charge_id, amount=str(t2.amount)))
        t2.refresh_from_db()
        self.assertEqual(r.status_code, 200)   # nunca 500, mesmo sem vaga
        self.assertEqual(t2.payment, Ticket.Payment.REVIEW)

    def test_evento_de_reembolso_desconhecido_nao_anula_bilhetes(self):
        """A iMali.Way não envia reembolsos; se um dia enviar, não anulamos nada sem querer."""
        t = self.comprar()
        confirm_payment(t, provider="imaliway", provider_reference="ref1")
        self.enviar(_webhook_body("PAYMENT.REFUNDED", t.provider_charge_id, status="REFUNDED"))
        t.refresh_from_db()
        self.assertEqual(t.payment, Ticket.Payment.PAID)


class ReconciliacaoTests(Base):
    def test_recupera_webhook_perdido(self):
        t = self.comprar()
        with patch("payments.imaliway.requests.request", return_value=_status("SUCCESS", "x")) as net:
            stats = reconcile_pending()
        t.refresh_from_db()
        self.assertEqual(t.payment, Ticket.Payment.PAID)       # a consulta não traz o valor: confia no pedido
        self.assertEqual(stats["confirmados"], 1)
        self.assertEqual(net.call_args.kwargs["params"]["partner_transaction_id"], t.provider_charge_id)

    def test_nao_confirma_o_que_continua_pendente(self):
        t = self.comprar()
        with patch("payments.imaliway.requests.request", return_value=_status("PENDING", "x")):
            reconcile_pending()
        t.refresh_from_db()
        self.assertEqual(t.payment, Ticket.Payment.PENDING)

    def test_expirado_no_gateway_liberta_a_vaga(self):
        t = self.comprar()
        antes = Price.objects.get(pk=self.price.pk).available
        with patch("payments.imaliway.requests.request", return_value=_status("EXPIRED", "x")):
            stats = reconcile_pending()
        t.refresh_from_db()
        self.assertEqual(t.payment, Ticket.Payment.FAILED)
        self.assertEqual(stats["falhados"], 1)
        self.assertEqual(Price.objects.get(pk=self.price.pk).available, antes + 1)

    def test_erro_do_gateway_conta_como_erro_e_nao_rebenta(self):
        t = self.comprar()
        with patch("payments.imaliway.requests.request", side_effect=requests.ConnectionError("x")):
            stats = reconcile_pending()
        t.refresh_from_db()
        self.assertEqual((t.payment, stats["erros"]), (Ticket.Payment.PENDING, 1))

    def test_ignora_bilhetes_de_outro_gateway(self):
        t = self.comprar()
        Ticket.objects.filter(pk=t.pk).update(provider="debitopay")
        with patch("payments.imaliway.requests.request") as net:
            stats = reconcile_pending()
        net.assert_not_called()
        self.assertEqual(stats["verificados"], 0)


class ChaveDeTesteTests(Base):
    """Um bilhete criado com uma chave etk_test_… nunca pode cobrar de
    verdade — tem de usar as credenciais da sandbox."""

    def setUp(self):
        super().setUp()
        _, raw_test = ApiKey.issue(self.org, environment=ApiKey.Environment.TEST)
        self.api_test = APIClient()
        self.api_test.credentials(HTTP_AUTHORIZATION=f"Bearer {raw_test}")

    @patch("payments.imaliway.requests.request", side_effect=_created)
    def test_compra_com_chave_test_usa_a_sandbox(self, request):
        r = self.api_test.post("/back/borrow/external/tickets",
                               {"priceId": self.price.id, "eventId": self.event.id,
                                "phone": "258841111111", "paymentMethod": "mpesa"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)

        self.assertTrue(Ticket.objects.get(pk=r.data["data"]["id"]).test_mode)
        args, kwargs = request.call_args
        self.assertTrue(args[1].startswith("https://sandbox.test/"))
        self.assertEqual(kwargs["headers"]["X-Client-ID"], "2222222222")
        token = kwargs["headers"]["Authorization"].removeprefix("Bearer ")
        self.assertEqual(_RSA.decrypt(base64.b64decode(token), padding.PKCS1v15()),
                         b"api-key-sandbox-teste")
        self.assertEqual(kwargs["json"]["store_account_number"], "290000001")

    @patch("payments.imaliway.requests.request", side_effect=_created)
    def test_compra_com_chave_live_nao_usa_a_sandbox(self, request):
        r = self.api.post("/back/borrow/external/tickets",
                          {"priceId": self.price.id, "eventId": self.event.id,
                           "phone": "258842222222", "paymentMethod": "mpesa"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)

        self.assertFalse(Ticket.objects.get(pk=r.data["data"]["id"]).test_mode)
        args, kwargs = request.call_args
        self.assertTrue(args[1].startswith("https://gateway.test/"))
        self.assertEqual(kwargs["headers"]["X-Client-ID"], "1111111111")
        self.assertEqual(kwargs["json"]["store_account_number"], "260000079")

    @patch("payments.imaliway.requests.request", side_effect=_created)
    def test_webhook_da_sandbox_confirma_bilhete_de_teste(self, request):
        r = self.api_test.post("/back/borrow/external/tickets",
                               {"priceId": self.price.id, "eventId": self.event.id,
                                "phone": "258841111111", "paymentMethod": "mpesa"}, format="json")
        t = Ticket.objects.get(pk=r.data["data"]["id"])
        body = _webhook_body("PAYMENT.SUCCESS", t.provider_charge_id, amount=str(t.amount))
        resp = self.enviar(body, secret=IMALIWAY_SANDBOX_TEST["WEBHOOK_SECRET"])
        t.refresh_from_db()
        self.assertEqual((resp.status_code, t.payment), (200, Ticket.Payment.PAID))


@override_settings(IMALIWAY=IMALIWAY_TEST, IMALIWAY_SANDBOX=IMALIWAY_SANDBOX_TEST)
class CheckImaliwayTests(SimpleTestCase):
    """O comando de diagnóstico, sem rede."""

    CMD = "payments.management.commands.check_imaliway"

    def run_cmd(self, *args, ip="203.0.113.7", tcp=None, request=None):
        from io import StringIO
        from django.core.management import call_command
        out = StringIO()
        ip_resp = Mock(text=ip)
        with patch(f"{self.CMD}.requests.get", return_value=ip_resp) as get, \
             patch(f"{self.CMD}.socket.create_connection", **({"side_effect": tcp} if tcp else {})), \
             patch("payments.imaliway.requests.request",
                   **({"side_effect": request} if request else {"return_value": _resp(
                       {"message_cod": 404, "message": "Invalid order"}, status=404)})) as net:
            call_command("check_imaliway", *args, stdout=out)
        return out.getvalue(), net, get

    def test_tudo_certo_mostra_o_ip_e_aceita_as_credenciais(self):
        out, net, _ = self.run_cmd()
        self.assertIn("203.0.113.7", out)
        self.assertIn("whitelist", out)
        self.assertIn("Credenciais aceites", out)
        self.assertIn("SANDBOX", out)
        self.assertEqual(net.call_args.args[0], "GET")                      # só leitura

    def test_nunca_imprime_segredos(self):
        out, _, _ = self.run_cmd()
        for secret in ("api-key-sandbox-teste", "whs_sandbox_teste", "BEGIN PUBLIC KEY"):
            self.assertNotIn(secret, out)

    def test_timeout_na_ligacao_diz_que_e_o_ip_e_nao_testa_credenciais(self):
        import socket
        out, net, _ = self.run_cmd(tcp=socket.timeout())
        self.assertIn("firewall", out)
        self.assertIn("Sem ligação", out)
        net.assert_not_called()

    def test_ligacao_recusada(self):
        out, net, _ = self.run_cmd(tcp=ConnectionRefusedError())
        self.assertIn("recusada", out)
        net.assert_not_called()

    def test_configuracao_em_falta_e_listada_sem_chamar_a_rede(self):
        with override_settings(IMALIWAY_SANDBOX={**IMALIWAY_SANDBOX_TEST, "API_KEY": ""}):
            out, net, _ = self.run_cmd()
        self.assertIn("IMALIWAY_SANDBOX_API_KEY em falta", out)
        net.assert_not_called()

    def test_token_invalido_explica_o_provavel_problema(self):
        out, _, _ = self.run_cmd(request=lambda *a, **k: _resp(
            {"message_cod": 500, "message": "Invalid token"}, status=500))
        self.assertIn("privateKey", out)

    def test_consulta_de_estado(self):
        respostas = [_resp({"message_cod": 404}, status=404), _status("SUCCESS", "X")]
        out, net, _ = self.run_cmd("--status", "ABCDEFGHIJKL", request=respostas)
        self.assertIn("succeeded", out)

    def test_pagamento_de_teste_cria_push_na_sandbox(self):
        def reply(method, url, **kw):
            if method == "POST":
                return _created(method, url, **kw)
            return _resp({"message_cod": 404}, status=404)
        out, net, _ = self.run_cmd("--pay", "841234567", "--amount", "10", request=reply)
        posts = [c for c in net.call_args_list if c.args[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertTrue(posts[0].args[1].startswith("https://sandbox.test/"))
        self.assertEqual(posts[0].kwargs["json"]["client_account_number"], "841234567")
        self.assertEqual(posts[0].kwargs["json"]["amount"], 10)
        self.assertIn("Push criado", out)
        self.assertIn("--status", out)

    def test_pagamento_live_exige_confirmacao_explicita(self):
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            self.run_cmd("--live", "--pay", "841234567")
