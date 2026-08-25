"""Adaptador Debito Pay.

    Reescrito com base na documentação real (payment-orchestrator API), que
    substitui as suposições CONTRATO-? do ficheiro anterior. Resolvido:

    CONTRATO-1  base_url = https://gyqoaningqhurhvdugne.supabase.co/functions/v1
                caminho de criação = POST /payment-orchestrator, body {"action": "process", ...}
    CONTRATO-2  Authorization: Bearer <secret key> — confirmado, sem alteração
    CONTRATO-3  amount é number (não string); campos: merchant_id, wallet_code,
                payment_method, amount, currency, source, source_id, phone
                (mobile money) ou return_url (cartões/PayFast), customer_*
    CONTRATO-4  resposta tem payment_id (usar como nossa `reference` — é o que
                o check-status e o webhook usam para identificar a cobrança),
                status ∈ {pending, success, failed, expired}, reference
                (referência do provedor, ex. transactionId) e checkout_url
                (só para visa_mastercard / payfast)
    CONTRATO-5  cabeçalho x-webhook-signature, HMAC-SHA256 em hex — confirmado
    CONTRATO-6  o valor assinado é o corpo cru (rawBody), não o JSON
                re-serializado — confirmado pelo exemplo em JS da doc

    Duas coisas que a doc deixa claras e que NÃO batem com o desenho anterior
    — não escondi isto, resolvi da forma mais segura e deixei marcado:

    * Não existe `callback_url` por pedido. O webhook é configurado uma vez
      em Settings → Webhooks do lado do gateway, não enviado no payload. O
      `callback_url` que o resto do sistema nos passa só faz sentido aqui
      como `return_url` — e só para os métodos que redirecionam o cliente
      (visa_mastercard, payfast). Para mobile money (mpesa/emola/mkesh) o
      valor é simplesmente ignorado.
    * Os eventos de webhook `payment.refunded` e `payment.chargeback` não
      têm equivalente em PENDING/SUCCEEDED/FAILED (base.py). Mapeei-os para
      PENDING (não altera o bilhete) e registo um aviso — mas isto significa
      que reembolsos e chargebacks não fazem nada automaticamente. O modelo
      Ticket já tem Payment.REFUNDED por usar; se isto importar para o
      negócio, precisa de um percurso próprio em services.py, não só aqui.

    Configuração esperada em settings.DEBITOPAY:
      BASE_URL        (default abaixo, normalmente não precisa mudar)
      SECRET_KEY       sk_live_... / sk_test_...
      WEBHOOK_SECRET
      MERCHANT_ID      uuid do merchant (Settings → API)
      WALLET_CODES     dict moeda -> wallet_code, ex. {"MZN": "12345", "ZAR": "67890"}
                        (a doc mostra wallets por moeda/método — se só tiver
                        uma carteira, pode usar WALLET_CODE em vez de WALLET_CODES)
      WALLET_CODE      fallback single-wallet, usado se WALLET_CODES não tiver a moeda
      TIMEOUT           opcional, default 30
      SIGNATURE_HEADER  opcional, default "X-Webhook-Signature"
"""

import hashlib
import hmac
import json
from decimal import Decimal
from typing import Mapping

import requests
from django.conf import settings

from .base import (
    FAILED,
    PENDING,
    SUCCEEDED,
    Charge,
    InvalidSignature,
    PaymentError,
    PaymentProvider,
    WebhookEvent,
)

DEFAULT_BASE_URL = "https://gyqoaningqhurhvdugne.supabase.co/functions/v1"

# Os nossos métodos internos podem não bater certo com os nomes do gateway —
# hoje só "card" precisa de tradução; os outros já usam o nome do gateway.
PAYMENT_METHOD_MAP = {
    "card": "visa_mastercard",
    "visa": "visa_mastercard",
    "mastercard": "visa_mastercard",
}

# CONTRATO-4 confirmado: só estes quatro estados existem na API.
STATUS_MAP = {
    "success": SUCCEEDED,
    "pending": PENDING,
    "failed": FAILED,
    "expired": FAILED,
}

# Os webhooks não trazem um campo "status" — só o nome do evento.
EVENT_STATUS_MAP = {
    "payment.completed": SUCCEEDED,
    "payment.failed": FAILED,
    # Sem equivalente em PENDING/SUCCEEDED/FAILED — ver nota no topo do ficheiro.
    "payment.refunded": PENDING,
    "payment.chargeback": PENDING,
}


class DebitoPayProvider(PaymentProvider):
    name = "debitopay"

    def __init__(self):
        cfg = settings.DEBITOPAY
        self.base_url = cfg.get("BASE_URL", DEFAULT_BASE_URL).rstrip("/")
        self.secret_key = cfg["SECRET_KEY"]
        self.webhook_secret = cfg["WEBHOOK_SECRET"]
        self.merchant_id = cfg["MERCHANT_ID"]
        self.wallet_codes = cfg.get("WALLET_CODES", {})
        self.wallet_code_fallback = cfg.get("WALLET_CODE")
        self.timeout = cfg.get("TIMEOUT", 30)
        self.signature_header = cfg.get("SIGNATURE_HEADER", "X-Webhook-Signature")

    # ------------------------------------------------------------------ HTTP
    def _headers(self, idempotency_key: str = "") -> dict:
        headers = {
            "Authorization": f"Bearer {self.secret_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if idempotency_key:
            # Opcional segundo a doc, mas evita duplicar cobranças em retries
            # de rede do lado do nosso servidor.
            headers["X-Idempotency-Key"] = idempotency_key
        return headers

    def _request(self, payload: dict, *, idempotency_key: str = "") -> dict:
        url = f"{self.base_url}/payment-orchestrator"
        try:
            resp = requests.post(
                url, headers=self._headers(idempotency_key), json=payload, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise PaymentError(f"Debito Pay inacessível: {exc}") from exc

        try:
            body = resp.json()
        except ValueError:
            body = {}

        if not resp.ok or body.get("success") is False:
            message = body.get("error") or f"HTTP {resp.status_code}"
            raise PaymentError(f"Debito Pay recusou o pedido: {message}")
        return body

    def _wallet_code(self, currency: str) -> str:
        code = self.wallet_codes.get(currency) or self.wallet_code_fallback
        if not code:
            raise PaymentError(f"Sem wallet_code configurado para a moeda {currency}.")
        return code

    # --------------------------------------------------------------- mapeamento
    @staticmethod
    def _charge_from_create(data: dict) -> Charge:
        status = STATUS_MAP.get(data.get("status", ""), PENDING)
        return Charge(
            reference=str(data.get("payment_id") or ""),
            status=status,
            amount=Decimal(str(data.get("amount"))) if data.get("amount") is not None else Decimal("0"),
            currency=(data.get("currency") or "MZN").upper(),
            checkout_url=data.get("checkout_url") or "",
            instructions=data.get("reference", ""),
            raw=data,
        )

    @staticmethod
    def _charge_from_status(payment: dict) -> Charge:
        status = STATUS_MAP.get(payment.get("status", ""), PENDING)
        return Charge(
            reference=str(payment.get("id") or ""),
            status=status,
            amount=Decimal(str(payment.get("amount"))) if payment.get("amount") is not None else Decimal("0"),
            currency=(payment.get("currency") or "MZN").upper(),
            raw=payment,
        )

    # ------------------------------------------------------------------- API
    def create_charge(self, *, amount, currency, reference, phone, method,
                      description, callback_url) -> Charge:
        gateway_method = PAYMENT_METHOD_MAP.get(method, method or "mpesa")
        payload = {
            "action": "process",
            "payment_method": gateway_method,
            "merchant_id": self.merchant_id,
            "wallet_code": self._wallet_code(currency),
            "amount": float(amount),
            "currency": currency,
            "source": "gateway",
            "source_id": str(reference),      # a nossa correlação, não idempotência HTTP
            "customer_phone": phone,
        }
        if gateway_method in ("mpesa", "emola", "mkesh"):
            payload["phone"] = phone
        else:
            # visa_mastercard / payfast redirecionam o cliente de volta —
            # não existe callback_url por pedido nesta API, só return_url.
            payload["return_url"] = callback_url

        body = self._request(payload, idempotency_key=str(reference))
        return self._charge_from_create(body)

    def fetch_charge(self, reference: str) -> Charge:
        body = self._request({"action": "check-status", "payment_id": reference})
        return self._charge_from_status(body.get("payment", {}))

    def parse_webhook(self, body: bytes, headers: Mapping[str, str]) -> WebhookEvent:
        signature = headers.get(self.signature_header) or headers.get(
            self.signature_header.lower(), ""
        )
        if not self._signature_ok(body, signature):
            raise InvalidSignature("Assinatura do webhook inválida.")

        payload = json.loads(body.decode())
        event_type = str(payload.get("event") or "")
        d = payload.get("data", {})
        amount = d.get("amount")
        payment_id = str(d.get("payment_id") or "")
        return WebhookEvent(
            # A doc não dá um id de evento próprio, só payment_id. Usar só o
            # payment_id como event_id faria ProviderEvent (único por
            # provider+event_id) descartar payment.completed seguido de
            # payment.refunded como "reenvio" — juntar o tipo evita isso,
            # mantendo a proteção contra reenvio do MESMO evento.
            event_id=f"{payment_id}:{event_type}",
            type=event_type,
            charge_reference=payment_id,
            status=EVENT_STATUS_MAP.get(event_type, PENDING),
            amount=Decimal(str(amount)) if amount is not None else None,
            currency=(d.get("currency") or "MZN").upper(),
            raw=payload,
        )

    def _signature_ok(self, body: bytes, signature: str) -> bool:
        if not signature:
            return False
        # CONTRATO-6 confirmado: assina o corpo cru, hex digest.
        digest = hmac.new(self.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(digest, signature)