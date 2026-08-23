"""Adaptador Debito Pay.

    ⚠️  A documentação pública em debitopay.com/api-docs é uma página de
    marketing: não publica endpoints, nomes de campos nem esquema de
    assinatura. O que está abaixo é a forma convencional destes gateways,
    com cada suposição marcada como CONTRATO-?. Confirme os seis pontos no
    dashboard/documentação real e ajuste — nada fora deste ficheiro muda.

    CONTRATO-1  base_url e caminho de criação de cobrança
    CONTRATO-2  cabeçalho de autenticação (Bearer? X-API-Key? par public/secret?)
    CONTRATO-3  nomes dos campos no pedido (amount em unidades ou cêntimos?)
    CONTRATO-4  nomes dos campos na resposta e valores de estado
    CONTRATO-5  cabeçalho e algoritmo da assinatura do webhook (hex ou base64?)
    CONTRATO-6  se o valor assinado é o corpo cru ou o JSON re-serializado
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

# CONTRATO-4: mapeamento dos estados do gateway para os nossos três estados.
STATUS_MAP = {
    "succeeded": SUCCEEDED, "success": SUCCEEDED, "successful": SUCCEEDED,
    "completed": SUCCEEDED, "paid": SUCCEEDED,
    "pending": PENDING, "processing": PENDING, "initiated": PENDING,
    "requires_action": PENDING,
    "failed": FAILED, "declined": FAILED, "cancelled": FAILED, "expired": FAILED,
}


class DebitoPayProvider(PaymentProvider):
    name = "debitopay"

    def __init__(self):
        cfg = settings.DEBITOPAY
        self.base_url = cfg["BASE_URL"].rstrip("/")   # CONTRATO-1
        self.secret_key = cfg["SECRET_KEY"]
        self.webhook_secret = cfg["WEBHOOK_SECRET"]
        self.timeout = cfg.get("TIMEOUT", 30)
        self.signature_header = cfg.get("SIGNATURE_HEADER", "X-Debito-Signature")

    # ------------------------------------------------------------------ HTTP
    def _headers(self) -> dict:
        # CONTRATO-2
        return {
            "Authorization": f"Bearer {self.secret_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        try:
            resp = requests.request(
                method, url, headers=self._headers(), json=payload, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise PaymentError(f"Debito Pay inacessível: {exc}") from exc

        try:
            body = resp.json()
        except ValueError:
            body = {}

        if not resp.ok:
            message = body.get("message") or body.get("error") or f"HTTP {resp.status_code}"
            raise PaymentError(f"Debito Pay recusou o pedido: {message}")
        return body

    # --------------------------------------------------------------- mapeamento
    @staticmethod
    def _to_charge(data: dict) -> Charge:
        # CONTRATO-4: alguns gateways aninham em {"data": {...}}
        d = data.get("data", data)
        raw_status = str(d.get("status") or d.get("state") or "").lower()
        amount = d.get("amount")
        return Charge(
            reference=str(d.get("id") or d.get("reference") or d.get("transaction_id") or ""),
            status=STATUS_MAP.get(raw_status, PENDING),
            amount=Decimal(str(amount)) if amount is not None else Decimal("0"),
            currency=(d.get("currency") or "MZN").upper(),
            checkout_url=d.get("checkout_url") or d.get("payment_url") or d.get("redirect_url") or "",
            instructions=d.get("instructions") or d.get("message") or "",
            raw=data,
        )

    # ------------------------------------------------------------------- API
    def create_charge(self, *, amount, currency, reference, phone, method,
                      description, callback_url) -> Charge:
        payload = {                                    # CONTRATO-3
            "amount": str(amount),                     # string evita erros de float
            "currency": currency,
            "reference": reference,                    # idempotência do nosso lado
            "customer": {"phone": phone},
            "payment_method": method or "mobile_money",
            "description": description[:140],
            "callback_url": callback_url,
        }
        return self._to_charge(self._request("POST", "/v1/charges", payload))

    def fetch_charge(self, reference: str) -> Charge:
        return self._to_charge(self._request("GET", f"/v1/charges/{reference}"))

    def parse_webhook(self, body: bytes, headers: Mapping[str, str]) -> WebhookEvent:
        signature = headers.get(self.signature_header) or headers.get(
            self.signature_header.lower(), ""
        )
        if not self._signature_ok(body, signature):
            raise InvalidSignature("Assinatura do webhook inválida.")

        payload = json.loads(body.decode())
        d = payload.get("data", payload)
        raw_status = str(d.get("status") or "").lower()
        amount = d.get("amount")
        return WebhookEvent(
            event_id=str(payload.get("id") or payload.get("event_id") or ""),
            type=str(payload.get("event") or payload.get("type") or ""),
            charge_reference=str(d.get("id") or d.get("reference") or ""),
            status=STATUS_MAP.get(raw_status, PENDING),
            amount=Decimal(str(amount)) if amount is not None else None,
            currency=(d.get("currency") or "MZN").upper(),
            raw=payload,
        )

    def _signature_ok(self, body: bytes, signature: str) -> bool:
        if not signature:
            return False
        digest = hmac.new(self.webhook_secret.encode(), body, hashlib.sha256)
        # CONTRATO-5: aceita hex e base64 até se confirmar qual deles é
        import base64
        candidates = (digest.hexdigest(), base64.b64encode(digest.digest()).decode())
        # compare_digest evita revelar o segredo por tempo de resposta
        return any(hmac.compare_digest(c, signature) for c in candidates)
