"""Debito Pay — único gateway usado. Sem abstração de "provider plugável":
antes havia base.py (interface) + registry.py (resolver por nome) +
debitopay.py (implementação) + fake.py (para testes), para um sistema que só
liga a um gateway real. Isto junta tudo num ficheiro, com funções diretas.

Duas particularidades deste gateway, que moldam a lógica abaixo:

1. M-Pesa confirma de forma SÍNCRONA — a resposta ao POST inicial já vem
   com status "success". Não há que esperar por webhook. e-Mola, mKesh e
   cartão continuam assíncronos (status "pending"), e dependem do webhook
   ou da reconciliação.

2. Cada método de pagamento tem a sua própria carteira (wallet_code) —
   settings.DEBITOPAY["WALLETS"].

O payment_id devolvido pela Debito Pay é o que guardamos como
Ticket.provider_charge_id — é ele que volta no webhook e no check-status.

CONTRATO A CONFIRMAR COM A DOCUMENTAÇÃO OFICIAL DA DEBITO PAY (não assumido,
copiado tal e qual do código anterior):
  - endpoint único: {BASE_URL}/payment-orchestrator
  - iniciar cobrança: {"action": "process", ...}
  - consultar estado: {"action": "check-status", "payment_id": ...}
  - assinatura do webhook: HMAC-SHA256 do body, no header X-Webhook-Signature
Se algum destes detalhes mudou do lado da Debito Pay, avisar antes de usar
isto em produção.
"""

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Mapping

import requests
from django.conf import settings

from .exceptions import InvalidSignature, PaymentDeclined, PaymentError, ProviderUnavailable

PROVIDER_NAME = "debitopay"

PENDING, SUCCEEDED, FAILED = "pending", "succeeded", "failed"

STATUS_MAP = {
    "success": SUCCEEDED,
    "pending": PENDING,
    "failed": FAILED,
    "expired": FAILED,
}

# Ticket.payment_method (o que o parceiro manda) -> wallet da Debito Pay.
# Aceitamos sinónimos comuns para não depender de o parceiro escrever
# exatamente "visa_mastercard".
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
    "payment.refunded": FAILED,
    "payment.chargeback": FAILED,
}


@dataclass(frozen=True)
class Charge:
    reference: str                    # payment_id da Debito Pay
    status: str                       # pending | succeeded | failed
    amount: Decimal
    currency: str
    checkout_url: str = ""
    instructions: str = ""
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class WebhookEvent:
    event_id: str                     # para idempotência (ProviderEvent)
    type: str                         # payment.completed, payment.failed...
    charge_reference: str
    status: str
    amount: Decimal | None
    currency: str
    raw: dict = field(default_factory=dict)


def _cfg() -> dict:
    return settings.DEBITOPAY


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_cfg()['SECRET_KEY']}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _post(path: str, payload: dict) -> dict:
    url = f"{_cfg()['BASE_URL'].rstrip('/')}{path}"
    try:
        resp = requests.post(url, headers=_headers(), json=payload, timeout=_cfg().get("TIMEOUT", 90))
    except requests.RequestException as exc:
        # Nunca chegou resposta — transporte, não negócio. Retryable.
        raise ProviderUnavailable(f"Debito Pay inacessível: {exc}") from exc

    try:
        body = resp.json()
    except ValueError:
        raise ProviderUnavailable(f"Debito Pay devolveu resposta ilegível (HTTP {resp.status_code}).")

    if not body.get("success", resp.ok):
        code = body.get("error", f"HTTP {resp.status_code}")
        # O gateway respondeu e avaliou o pedido — é uma recusa (saldo
        # insuficiente, método inválido, etc.), não uma falha de comunicação.
        raise PaymentDeclined(f"Debito Pay recusou o pedido: {code}", code=code)
    return body


def _method_for(method: str | None) -> str:
    wallets = _cfg()["WALLETS"]
    key = (method or "").strip().lower()
    resolved = METHOD_ALIASES.get(key, _cfg().get("DEFAULT_METHOD", "mpesa"))
    if resolved not in wallets or not wallets[resolved]:
        raise PaymentError(
            f"Sem wallet_code configurada para o método '{resolved}'. "
            f"Defina DEBITOPAY_WALLET_{resolved.upper()} no ambiente."
        )
    return resolved


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


def _to_charge(data: dict) -> Charge:
    raw_status = str(data.get("status") or "").lower()
    return Charge(
        reference=str(data.get("payment_id") or ""),
        status=STATUS_MAP.get(raw_status, PENDING),
        amount=Decimal(str(data.get("amount"))) if data.get("amount") is not None else Decimal("0"),
        currency=(data.get("currency") or "MZN").upper(),
        checkout_url=data.get("checkout_url") or "",
        instructions=_instructions(data),
        raw=data,
    )


def create_charge(*, amount, currency, reference, phone, method, description, callback_url) -> Charge:
    """Inicia a cobrança. `reference` (o nosso Ticket.id) viaja como source_id,
    rastreável do lado da Debito Pay."""
    method_key = _method_for(method)
    cfg = _cfg()
    payload = {
        "action": "process",
        "payment_method": method_key,
        "merchant_id": cfg["MERCHANT_ID"],
        "wallet_code": cfg["WALLETS"][method_key],
        "amount": float(amount),
        "currency": currency,
        "source": "etk-api",
        "source_id": reference,
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

    body = _post("/payment-orchestrator", payload)
    return _to_charge(body)


def fetch_charge(reference: str) -> Charge:
    """Lê o estado atual. Usado na reconciliação, quando o webhook se perde."""
    body = _post("/payment-orchestrator", {"action": "check-status", "payment_id": reference})
    payment = body.get("payment", body)
    return _to_charge(payment)


def parse_webhook(body: bytes, headers: Mapping[str, str]) -> WebhookEvent:
    """Valida a assinatura e devolve o evento. Levanta InvalidSignature."""
    cfg = _cfg()
    signature_header = cfg.get("SIGNATURE_HEADER", "X-Webhook-Signature")
    signature = headers.get(signature_header) or headers.get(signature_header.lower(), "")
    if not _signature_ok(body, signature, cfg["WEBHOOK_SECRET"]):
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


def _signature_ok(body: bytes, signature: str, webhook_secret: str) -> bool:
    if not signature or not webhook_secret:
        return False
    expected = hmac.new(webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
