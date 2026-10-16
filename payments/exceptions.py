"""Exceções do pagamento.

Antes viviam em payments/providers/base.py, ao lado da interface PaymentProvider.
Como só existe a iMali.Way, a interface deixou de fazer sentido — mas as
exceções continuam úteis, porque separam três situações que a view trata de
forma diferente:

- InvalidSignature   → 401, não mexe na base de dados.
- ProviderUnavailable → falha de transporte (timeout, DNS, 5xx). Vale a pena
                        repetir mais tarde; a vaga é libertada já.
- PaymentDeclined    → a iMali.Way recebeu e recusou o pedido (saldo, método
                        inválido, etc.). Repetir o mesmo pedido não muda nada.
"""


class PaymentError(Exception):
    """Falha genérica ao falar com a iMali.Way."""


class InvalidSignature(PaymentError):
    """Webhook não assinado pela iMali.Way — trata-se como ataque, não como erro."""


class ProviderUnavailable(PaymentError):
    """Falha de transporte (timeout, DNS, 5xx, resposta ilegível). Retryable."""


class PaymentDeclined(PaymentError):
    """A iMali.Way avaliou o pedido e recusou-o. Quem tem de agir é o cliente."""

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code
