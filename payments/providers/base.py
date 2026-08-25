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
                      callback_url: str, customer_name: str = "",
                      customer_email: str = "", customer_phone: str = "") -> Charge:
        """Inicia a cobrança. `reference` é o nosso id — serve de chave de idempotência.

        customer_name/customer_email/customer_phone são opcionais e servem
        para gateways que precisam destes dados no Hosted Checkout (ex.:
        cartões, PayFast). Providers que não precisam podem ignorá-los.
        """

    @abstractmethod
    def fetch_charge(self, reference: str) -> Charge:
        """Lê o estado atual. Usado na reconciliação, quando o webhook se perde."""

    @abstractmethod
    def parse_webhook(self, body: bytes, headers: Mapping[str, str]) -> WebhookEvent:
        """Valida a assinatura e devolve o evento. Levanta InvalidSignature."""