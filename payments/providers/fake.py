"""Gateway falso: permite testar o fluxo completo sem credenciais reais."""

import hashlib
import hmac
import json
import uuid
from decimal import Decimal
from typing import Mapping

from .base import (
    FAILED, PENDING, SUCCEEDED,
    Charge, InvalidSignature, PaymentProvider, WebhookEvent,
)

WEBHOOK_SECRET = "fake-webhook-secret"


class FakeProvider(PaymentProvider):
    name = "fake"

    def __init__(self):
        self.charges: dict[str, Charge] = {}

    def create_charge(self, *, amount, currency, reference, phone, method,
                      description, callback_url) -> Charge:
        charge = Charge(
            reference=f"chg_{uuid.uuid4().hex[:12]}",
            status=PENDING, amount=Decimal(str(amount)), currency=currency,
            instructions="Confirme o pagamento no seu telemóvel.",
            raw={"reference": reference, "phone": phone},
        )
        self.charges[charge.reference] = charge
        return charge

    def fetch_charge(self, reference: str) -> Charge:
        return self.charges[reference]

    def settle(self, reference: str, status: str = SUCCEEDED) -> Charge:
        """Só no falso: simula o cliente a confirmar (ou recusar) no telemóvel."""
        c = self.charges[reference]
        self.charges[reference] = Charge(
            reference=c.reference, status=status, amount=c.amount,
            currency=c.currency, raw=c.raw,
        )
        return self.charges[reference]

    def build_webhook(self, reference: str, status: str = SUCCEEDED,
                      amount: Decimal | None = None, event_id: str | None = None):
        c = self.charges[reference]
        payload = {
            "id": event_id or f"evt_{uuid.uuid4().hex[:12]}",
            "event": f"payment.{status}",
            "data": {
                "id": reference,
                "status": status,
                "amount": str(amount if amount is not None else c.amount),
                "currency": c.currency,
            },
        }
        body = json.dumps(payload).encode()
        sig = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        return body, {"X-Debito-Signature": sig}

    def parse_webhook(self, body: bytes, headers: Mapping[str, str]) -> WebhookEvent:
        sig = headers.get("X-Debito-Signature", "")
        expected = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            raise InvalidSignature("Assinatura inválida.")
        p = json.loads(body.decode())
        d = p["data"]
        return WebhookEvent(
            event_id=p["id"], type=p["event"], charge_reference=d["id"],
            status=d["status"], amount=Decimal(str(d["amount"])),
            currency=d["currency"], raw=p,
        )
