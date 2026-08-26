"""Adaptador Debito Pay — contrato confirmado pela documentação oficial.

    https://gyqoaningqhurhvdugne.supabase.co/functions/v1/payment-orchestrator

Duas particularidades deste gateway que não são o padrão de mercado, e que
moldam o resto deste ficheiro:

1. M-Pesa confirma de forma SÍNCRONA — a própria resposta ao POST inicial já
   vem com `status: "success"`. Não há que esperar por um webhook. e-Mola,
   mKesh e cartões continuam assíncronos (`status: "pending"`).

2. Cada método de pagamento tem a sua própria carteira (`wallet_code`) —
   não é a mesma carteira para M-Pesa, e-Mola e cartão. Isso mapeia-se em
   `settings.DEBITOPAY["WALLETS"]`.

O `payment_id` devolvido pelo gateway é o que usamos como `Charge.reference`
— é ele que aparece depois no webhook e no `check-status`.
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

STATUS_MAP = {
    "success": SUCCEEDED,
    "pending": PENDING,
    "failed": FAILED,
    "expired": FAILED,
}

# O nosso vocabulário interno (o que vem em Ticket.payment_method) para o da
# Debito Pay. Aceitamos sinónimos comuns para não depender de o cliente
# escrever exatamente "visa_mastercard".
METHOD_ALIASES = {
    "mpesa": "mpesa",
    "m-pesa": "mpesa",
    "emola": "emola",
    "e-mola": "emola",
    "mkesh": "mkesh",
    "m-kesh": "mkesh",
    "card": "visa_mastercard",
    "cartao": "visa_mastercard",
    "cartão": "visa_mastercard",
    "visa": "visa_mastercard",
    "mastercard": "visa_mastercard",
    "visa_mastercard": "visa_mastercard",
    "payfast": "payfast",
}

# event do webhook -> o nosso estado de três valores
EVENT_STATUS = {
    "payment.completed": SUCCEEDED,
    "payment.failed": FAILED,
    "payment.refunded": FAILED,     # tratado como falha para efeitos de bilhete;
    "payment.chargeback": FAILED,   # a diferenciação fica ao nível do relatório
}


class DebitoPayProvider(PaymentProvider):
    name = "debitopay"

    def __init__(self):
        cfg = settings.DEBITOPAY
        self.base_url = cfg["BASE_URL"].rstrip("/")
        self.secret_key = cfg["SECRET_KEY"]
        self.webhook_secret = cfg["WEBHOOK_SECRET"]
        self.merchant_id = cfg["MERCHANT_ID"]
        self.wallets = cfg["WALLETS"]
        self.default_method = cfg.get("DEFAULT_METHOD", "mpesa")
        self.timeout = cfg.get("TIMEOUT", 30)
        self.signature_header = cfg.get("SIGNATURE_HEADER", "X-Webhook-Signature")

    # ------------------------------------------------------------------ HTTP
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.secret_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _post(self, path: str, payload: dict) -> dict:
        url = f"{self.base_url}{path}"
        try:
            resp = requests.post(
                url, headers=self._headers(), json=payload, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise PaymentError(f"Debito Pay inacessível: {exc}") from exc

        try:
            body = resp.json()
        except ValueError:
            raise PaymentError(f"Debito Pay devolveu resposta ilegível (HTTP {resp.status_code}).")

        if not body.get("success", resp.ok):
            code = body.get("error", f"HTTP {resp.status_code}")
            raise PaymentError(f"Debito Pay recusou o pedido: {code}")
        return body

    def _method_for(self, method: str | None) -> str:
        key = (method or "").strip().lower()
        resolved = METHOD_ALIASES.get(key, self.default_method)
        if resolved not in self.wallets or not self.wallets[resolved]:
            raise PaymentError(
                f"Sem wallet_code configurada para o método '{resolved}'. "
                f"Defina DEBITOPAY_WALLET_{resolved.upper()} no ambiente."
            )
        return resolved

    # --------------------------------------------------------------- mapeamento
    def _to_charge(self, data: dict) -> Charge:
        raw_status = str(data.get("status") or "").lower()
        return Charge(
            reference=str(data.get("payment_id") or ""),
            status=STATUS_MAP.get(raw_status, PENDING),
            amount=Decimal(str(data.get("amount"))) if data.get("amount") is not None else Decimal("0"),
            currency=(data.get("currency") or "MZN").upper(),
            checkout_url=data.get("checkout_url") or "",
            instructions=self._instructions(data),
            raw=data,
        )

    @staticmethod
    def _instructions(data: dict) -> str:
        status = str(data.get("status") or "").lower()
        method = str(data.get("payment_method") or "").lower()
        if status == "success":
            return "Pagamento confirmado."
        if method in ("emola", "mkesh"):
            return "Confirme o pagamento no seu telemóvel."
        if data.get("checkout_url"):
            return "Complete o pagamento na página que se vai abrir."
        return "A processar."

    # ------------------------------------------------------------------- API
    def create_charge(self, *, amount, currency, reference, phone, method,
                      description, callback_url) -> Charge:
        method_key = self._method_for(method)
        payload = {
            "action": "process",
            "payment_method": method_key,
            "merchant_id": self.merchant_id,
            "wallet_code": self.wallets[method_key],
            "amount": float(amount),
            "currency": currency,
            "source": "etk-api",
            "source_id": reference,       # o nosso Ticket.id — rastreável do lado deles
        }
        if method_key in ("mpesa", "emola", "mkesh"):
            if not phone:
                raise PaymentError(f"Telefone é obrigatório para o método '{method_key}'.")
            payload["phone"] = phone
        else:
            # visa_mastercard / payfast: cartão, sem telefone obrigatório
            payload["return_url"] = callback_url
            if description:
                payload["customer_name"] = description[:140]

        body = self._post("/payment-orchestrator", payload)
        return self._to_charge(body)

    def fetch_charge(self, reference: str) -> Charge:
        body = self._post("/payment-orchestrator", {
            "action": "check-status", "payment_id": reference,
        })
        payment = body.get("payment", body)
        return self._to_charge(payment)

    def parse_webhook(self, body: bytes, headers: Mapping[str, str]) -> WebhookEvent:
        signature = headers.get(self.signature_header) or headers.get(
            self.signature_header.lower(), ""
        )
        if not self._signature_ok(body, signature):
            raise InvalidSignature("Assinatura do webhook inválida.")

        payload = json.loads(body.decode())
        event_type = str(payload.get("event") or "")
        data = payload.get("data", {})
        status = EVENT_STATUS.get(event_type, PENDING)
        amount = data.get("amount")

        return WebhookEvent(
            event_id=f"{data.get('payment_id', '')}:{event_type}",
            type=event_type,
            charge_reference=str(data.get("payment_id") or ""),
            status=status,
            amount=Decimal(str(amount)) if amount is not None else None,
            currency=(data.get("currency") or "MZN").upper(),
            raw=payload,
        )

    def _signature_ok(self, body: bytes, signature: str) -> bool:
        if not signature or not self.webhook_secret:
            return False
        expected = hmac.new(self.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)
