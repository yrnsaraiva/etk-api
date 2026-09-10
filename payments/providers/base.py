"""Porta de pagamentos: o resto do sistema só conhece esta interface.

Trocar de gateway (ou correr testes) é trocar a implementação, não mexer nas
regras de negócio.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Mapping


class PaymentError(Exception):
    """Falha ao comunicar com o gateway."""


class InvalidSignature(PaymentError):
    """Webhook não assinado pelo gateway — trata-se como ataque, não como erro."""


class ProviderUnavailable(PaymentError):
    """Falha de transporte ao falar com o gateway (timeout, DNS, 5xx, resposta
    ilegível). Nada no pedido está errado — vale a pena tentar de novo."""


class PaymentDeclined(PaymentError):
    """O gateway recebeu e avaliou o pedido, e recusou-o (saldo insuficiente,
    método inválido, etc.). Repetir o mesmo pedido não muda o resultado —
    quem tem de agir é o cliente (trocar de saldo, de método, de cartão)."""

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code


PENDING, SUCCEEDED, FAILED = "pending", "succeeded", "failed"


@dataclass(frozen=True)
class Charge:
    reference: str                    # id da cobrança no gateway
    status: str                       # pending | succeeded | failed
    amount: Decimal
    currency: str
    checkout_url: str = ""            # se o gateway usar página hospedada
    instructions: str = ""            # ex.: "Confirme no seu telemóvel"
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class WebhookEvent:
    event_id: str                     # para idempotência
    type: str                         # payment.succeeded, payment.failed...
    charge_reference: str
    status: str
    amount: Decimal | None
    currency: str
    raw: dict = field(default_factory=dict)


class PaymentProvider(ABC):
    name: str = "base"

    @abstractmethod
    def create_charge(self, *, amount: Decimal, currency: str, reference: str,
                      phone: str, method: str, description: str,
                      callback_url: str) -> Charge:
        """Inicia a cobrança. `reference` é o nosso id — serve de chave de idempotência."""

    @abstractmethod
    def fetch_charge(self, reference: str) -> Charge:
        """Lê o estado atual. Usado na reconciliação, quando o webhook se perde."""

    @abstractmethod
    def parse_webhook(self, body: bytes, headers: Mapping[str, str]) -> WebhookEvent:
        """Valida a assinatura e devolve o evento. Levanta InvalidSignature."""