"""iMali.Way (Paytek) — o gateway de pagamento. Substitui a Debito Pay.

Contrato: "iMali Payment Gateway API — Partner Integration", v1.4a (22-04-2026).
Sem abstração de "provider plugável": funções diretas, usadas por
payments/services.py.

O que molda a lógica abaixo:

1. **Autenticação.** A `privateKey` é a `api_key` cifrada com a `publicKey` da
   Paytek (RSA, padding PKCS#1 v1.5), enviada em `Authorization: Bearer …` com
   `X-Client-ID`. O texto cifrado vai em base64 (a documentação mostra-o numa
   imagem — **confirmar na sandbox**). A cifra é aleatória: gera-se uma chave
   nova por pedido.

2. **Push payment.** `POST {BASE_URL}/payments` com `payment_type="push"`,
   `transaction_type="C2B"` e `payment_method` mpesa | emola | mkesh | imali.
   O estado inicial é sempre PENDING: o cliente aprova no telemóvel. Não há
   confirmação síncrona.

3. **`partner_transaction_id`** tem exatamente 12 caracteres alfanuméricos e é
   o que identifica a cobrança (no webhook e na consulta de estado) — é o que
   guardamos como `Ticket.provider_charge_id`. Derivamo-lo do id do bilhete, de
   forma determinística: repetir o pedido para o mesmo bilhete nunca cria uma
   segunda cobrança (o gateway responde 406, "já em uso").

4. **Valor mínimo 10 MT.** Abaixo disso o gateway recusa; recusamos antes, sem
   chamar a rede.

5. **Webhooks** (PAYMENT.PENDING | SUCCESS | FAILED), assinados com
   HMAC-SHA256 de `"{timestamp}.{corpo}"` e o segredo do webhook, com janela
   anti-replay de 5 minutos. A documentação é inconsistente nos nomes dos
   cabeçalhos (X-Webhook-Signature/X-Webhook-Timestamp nos exemplos,
   X-Signature/X-Timestamp na tabela) e o exemplo traz o prefixo `sha256=`:
   aceitamos as duas grafias e com ou sem prefixo.

6. **Não há reembolsos nem estorno automáticos**: a API só tem transferências
   B2C (saída de dinheiro). O reembolso é feito à mão, fora do sistema, e
   marcado no /admin/.

7. **Sem cartão.** Só carteiras móveis (e conta iMali).

IP: a Paytek só aceita pedidos de um IP previamente registado (whitelist).
"""

import base64
import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Mapping
from zoneinfo import ZoneInfo

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
from django.conf import settings
from django.utils import timezone

from .exceptions import InvalidSignature, PaymentDeclined, PaymentError, ProviderUnavailable

PROVIDER_NAME = "imaliway"

PENDING, SUCCEEDED, FAILED = "pending", "succeeded", "failed"

MIN_AMOUNT = Decimal("10")           # MT
WEBHOOK_TOLERANCE_SECONDS = 300      # anti-replay (documentação, 7.7.6)

STATUS_MAP = {
    "SUCCESS": SUCCEEDED,
    "PENDING": PENDING,
    "FAILED": FAILED,
    "EXPIRED": FAILED,               # expirou sem pagar: a vaga volta
}

# Ticket.payment_method (o que o parceiro manda) -> payment_method da iMali.Way.
METHOD_ALIASES = {
    "mpesa": "mpesa", "m-pesa": "mpesa",
    "emola": "emola", "e-mola": "emola",
    "mkesh": "mkesh", "m-kesh": "mkesh",
    "imali": "imali", "i-mali": "imali",
}
UNSUPPORTED_METHODS = {"card", "cartao", "cartão", "visa", "mastercard", "visa_mastercard", "payfast"}

# Códigos de erro que significam "o gateway avaliou e recusou" (documentação, 4.2).
# 500 (token inválido) e 405 são configuração/integração, não decisão sobre o cliente.
DECLINE_CODES = {401, 402, 404, 406, 407, 408, 409, 422, 501, 502}

_MAPUTO = ZoneInfo("Africa/Maputo")


@dataclass(frozen=True)
class Charge:
    reference: str                    # partner_transaction_id (12 caracteres)
    status: str                       # pending | succeeded | failed
    amount: Decimal | None            # None quando o gateway não o devolve (consulta de estado)
    currency: str
    checkout_url: str = ""
    instructions: str = ""
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class WebhookEvent:
    event_id: str                     # para idempotência (ProviderEvent)
    type: str                         # PAYMENT.SUCCESS, PAYMENT.FAILED, PAYMENT.PENDING
    charge_reference: str             # partner_transaction_id
    status: str
    amount: Decimal | None
    currency: str
    raw: dict = field(default_factory=dict)


# ── configuração ──────────────────────────────────────────────────────────────

def _cfg(sandbox: bool = False) -> dict:
    return settings.IMALIWAY_SANDBOX if sandbox else settings.IMALIWAY


def _need(cfg: dict, key: str, env: str) -> str:
    value = cfg.get(key)
    if not value:
        raise PaymentError(f"iMali.Way sem configuração: defina {env} no ambiente.")
    return value


# ── autenticação ──────────────────────────────────────────────────────────────

def _load_public_key(pem: str):
    # Variáveis de ambiente costumam trazer o PEM numa linha, com "\n" literais.
    pem = pem.replace("\\n", "\n").strip()
    try:
        return serialization.load_pem_public_key(pem.encode())
    except Exception as exc:
        raise PaymentError(f"iMali.Way: publicKey inválida ({exc}).") from exc


def private_key(sandbox: bool = False) -> str:
    """`privateKey` do pedido: a api_key cifrada com a publicKey (RSA PKCS#1 v1.5), em base64."""
    cfg = _cfg(sandbox)
    prefix = "IMALIWAY_SANDBOX_" if sandbox else "IMALIWAY_"
    api_key = _need(cfg, "API_KEY", prefix + "API_KEY")
    public = _load_public_key(_need(cfg, "PUBLIC_KEY", prefix + "PUBLIC_KEY"))
    return base64.b64encode(public.encrypt(api_key.encode(), padding.PKCS1v15())).decode()


def _headers(sandbox: bool = False) -> dict:
    cfg = _cfg(sandbox)
    prefix = "IMALIWAY_SANDBOX_" if sandbox else "IMALIWAY_"
    return {
        "Authorization": f"Bearer {private_key(sandbox)}",
        "X-Client-ID": str(_need(cfg, "CLIENT_ID", prefix + "CLIENT_ID")),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


# ── pedidos ───────────────────────────────────────────────────────────────────

def _request(method: str, path: str, *, sandbox: bool = False, json_body=None, params=None) -> dict:
    cfg = _cfg(sandbox)
    url = f"{cfg['BASE_URL'].rstrip('/')}{path}"
    try:
        resp = requests.request(method, url, headers=_headers(sandbox), json=json_body,
                                params=params, timeout=cfg.get("TIMEOUT", 60))
    except requests.RequestException as exc:
        # Nunca chegou resposta — transporte, não negócio. Retryable.
        raise ProviderUnavailable(f"iMali.Way inacessível: {exc}") from exc

    try:
        body = resp.json()
    except ValueError:
        raise ProviderUnavailable(f"iMali.Way devolveu resposta ilegível (HTTP {resp.status_code}).")
    if not isinstance(body, dict):
        raise ProviderUnavailable("iMali.Way devolveu uma resposta inesperada.")

    if resp.ok and "data" in body:
        return body

    # O código vem no corpo (message_cod / code); sem ele, vale o do HTTP.
    has_body_code = any(k in body for k in ("message_cod", "code"))
    code = _error_code(resp.status_code, body)
    message = _error_message(body) or f"HTTP {resp.status_code}"
    if code in DECLINE_CODES and (has_body_code or resp.status_code < 500):
        # O gateway respondeu e avaliou o pedido — é uma recusa, não uma falha de comunicação.
        raise PaymentDeclined(f"iMali.Way recusou o pedido: {message}", code=str(code))
    if resp.status_code >= 500 and not has_body_code:
        raise ProviderUnavailable(f"iMali.Way indisponível (HTTP {resp.status_code}).")
    raise PaymentError(f"iMali.Way: erro {code}: {message}")


def _error_code(http_status: int, body: dict) -> int:
    for key in ("message_cod", "code"):
        try:
            return int(body[key])
        except (KeyError, TypeError, ValueError):
            continue
    return http_status


def _error_message(body: dict) -> str:
    if isinstance(body.get("errors"), dict):
        parts = []
        for field_name, value in body["errors"].items():
            parts.append(f"{field_name}: {' '.join(value) if isinstance(value, list) else value}")
        return "; ".join(parts)
    return str(body.get("message") or body.get("error") or "")


# ── cobranças ─────────────────────────────────────────────────────────────────

def partner_transaction_id(ticket_id: str) -> str:
    """12 caracteres alfanuméricos, estáveis para o mesmo bilhete."""
    digest = hashlib.sha256(ticket_id.encode()).digest()
    return base64.b32encode(digest).decode()[:12]            # A–Z e 2–7


def _method_for(method: str | None, *, sandbox: bool = False) -> str:
    key = (method or "").strip().lower()
    if not key:
        return _cfg(sandbox).get("DEFAULT_METHOD", "mpesa")
    if key in UNSUPPORTED_METHODS:
        raise PaymentDeclined("Pagamento por cartão não está disponível. Use M-Pesa, e-Mola ou mKesh.",
                              code="unsupported_method")
    if key not in METHOD_ALIASES:
        raise PaymentDeclined(f"Método de pagamento não suportado: '{method}'.", code="unsupported_method")
    return METHOD_ALIASES[key]


def _local_number(phone: str | None) -> str:
    """258841234567 -> 841234567 (a iMali.Way quer os 9 dígitos, sem indicativo)."""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 12 and digits.startswith("258"):
        digits = digits[3:]
    if not re.fullmatch(r"\d{9}", digits):
        raise PaymentError("Telefone inválido para o pagamento (esperados 9 dígitos).")
    return digits


def _expiration(expires_at, sandbox: bool) -> str:
    """Limite do push, na hora de Maputo, como a API exige (Y-m-d H:i:s).

    Nunca depois de a reserva do bilhete expirar, e nunca mais de PUSH_EXPIRATION_MINUTES.
    """
    limit = timezone.now() + timedelta(minutes=_cfg(sandbox).get("PUSH_EXPIRATION_MINUTES", 5))
    if expires_at and expires_at < limit:
        limit = expires_at
    return limit.astimezone(_MAPUTO).strftime("%Y-%m-%d %H:%M:%S")


def _to_charge(data: dict, *, reference: str = "") -> Charge:
    status = STATUS_MAP.get(str(data.get("status") or "").upper(), PENDING)
    amount = data.get("amount")
    try:
        amount = Decimal(str(amount)) if amount is not None else None
    except InvalidOperation:
        amount = None
    return Charge(
        reference=str(data.get("partner_transaction_id") or reference).strip(),
        status=status,
        amount=amount,
        currency="MZN",
        instructions="Pagamento confirmado." if status == SUCCEEDED
        else "Confirme o pagamento no seu telemóvel.",
        raw=data,
    )


def create_charge(*, amount, currency, reference, phone, method, description=None, expires_at=None,
                  sandbox: bool = False) -> Charge:
    """Inicia o push payment. `reference` é o nosso Ticket.id.

    `sandbox=True` (bilhete criado com uma chave `etk_test_…`) usa
    `settings.IMALIWAY_SANDBOX` — sem isto, uma chave de teste cobrava dinheiro
    real como qualquer outra.
    """
    amount = Decimal(str(amount))
    if amount < MIN_AMOUNT:
        raise PaymentDeclined(f"Valor mínimo de pagamento: {MIN_AMOUNT} MT.", code="min_amount")
    if currency and currency.upper() != "MZN":
        raise PaymentDeclined(f"Moeda não suportada: {currency}.", code="currency")

    cfg = _cfg(sandbox)
    prefix = "IMALIWAY_SANDBOX_" if sandbox else "IMALIWAY_"
    pid = partner_transaction_id(reference)
    payload = {
        "client_account_number": _local_number(phone),
        "amount": float(amount.quantize(Decimal("0.01"))),
        "store_account_number": str(_need(cfg, "STORE_ACCOUNT_NUMBER", prefix + "STORE_ACCOUNT_NUMBER")),
        "partner_transaction_id": pid,
        "payment_method": _method_for(method, sandbox=sandbox),
        "payment_type": "push",
        "transaction_type": "C2B",
        "expiration_datetime": _expiration(expires_at, sandbox),
    }

    try:
        body = _request("POST", "/payments", sandbox=sandbox, json_body=payload)
    except PaymentDeclined as exc:
        if exc.code == "406":
            # "partner_transaction_id já em uso": este bilhete já tem cobrança.
            # Repetir o pedido não a duplica — devolve o estado atual dela.
            return fetch_charge(pid, sandbox=sandbox)
        raise
    return _to_charge(body["data"], reference=pid)


def fetch_charge(reference: str, *, sandbox: bool = False) -> Charge:
    """Lê o estado atual. Usado na reconciliação, quando o webhook se perde.

    A consulta só devolve estado e tempo restante — não o valor.
    """
    body = _request("GET", "/payments/status", sandbox=sandbox,
                    params={"payment_type": "push", "partner_transaction_id": reference})
    data = dict(body["data"])
    data.setdefault("partner_transaction_id", reference)
    return _to_charge(data, reference=reference)


# ── webhook ───────────────────────────────────────────────────────────────────

def _header(headers: Mapping[str, str], *names: str) -> str:
    for name in names:
        value = headers.get(name) or headers.get(name.lower()) or headers.get(name.upper())
        if value:
            return value.strip()
    return ""


def _signature_ok(body: bytes, timestamp: str, signature: str, secret: str) -> bool:
    if not signature or not secret:
        return False
    signature = signature.removeprefix("sha256=").strip().lower()
    signed = f"{timestamp}.".encode() + body if timestamp else body
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def parse_webhook(body: bytes, headers: Mapping[str, str]) -> WebhookEvent:
    """Valida a assinatura e devolve o evento. Levanta InvalidSignature.

    O mesmo endpoint recebe webhooks das duas contas (live e sandbox) — não há
    como saber qual delas antes de validar, por isso tenta o segredo da live
    e, se não bater, o da sandbox.
    """
    timestamp = _header(headers, "X-Webhook-Timestamp", "X-Timestamp")
    signature = _header(headers, "X-Webhook-Signature", "X-Signature")
    secrets = [s for s in (_cfg()["WEBHOOK_SECRET"], _cfg(sandbox=True)["WEBHOOK_SECRET"]) if s]
    if not any(_signature_ok(body, timestamp, signature, s) for s in secrets):
        raise InvalidSignature("Assinatura do webhook inválida.")

    if timestamp:
        try:
            age = abs(time.time() - float(timestamp))
        except ValueError:
            raise InvalidSignature("Timestamp do webhook inválido.")
        if age > WEBHOOK_TOLERANCE_SECONDS:
            raise InvalidSignature("Webhook expirado (fora da janela de 5 minutos).")

    try:
        payload = json.loads(body.decode())
    except ValueError:
        raise InvalidSignature("Corpo do webhook ilegível.")
    data = payload.get("data") or {}
    event_type = str(payload.get("type") or "").upper()

    # Só interessam cobranças (C2B). Transferências B2C (saída) não mexem em bilhetes.
    is_collection = str(payload.get("payment_type") or data.get("payment_type") or "C2B").upper() == "C2B"
    status = STATUS_MAP.get(str(data.get("status") or "").upper(), PENDING) if is_collection else PENDING

    amount = data.get("amount")
    try:
        amount = Decimal(str(amount)) if amount is not None else None
    except InvalidOperation:
        amount = None

    event_id = str(payload.get("id") or _header(headers, "X-Webhook-Id") or "")
    reference = str(data.get("partner_transaction_id") or "").strip()
    return WebhookEvent(
        event_id=event_id or f"{reference}:{event_type}",
        type=event_type,
        charge_reference=reference if is_collection else "",
        status=status,
        amount=amount,
        currency="MZN",
        raw=payload,
    )
