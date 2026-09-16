#!/usr/bin/env python3
"""Teste rápido do payment-orchestrator — tudo hardcoded, edita e corre."""

import json
import requests

URL = "https://gyqoaningqhurhvdugne.supabase.co/functions/v1/payment-orchestrator"
API_KEY = "sk_live_EWpLPCRce2AgBOlukP6Jhj3qXno3NdMR"

payload = {
    "action": "process",
    "payment_method": "mpesa",
    "merchant_id": "e5d6a8d9-e5f6-476e-b2b8-13cc3ef19788",
    "wallet_code": "58492",
    "amount": 500,
    "currency": "MZN",
    "phone": "258845343113",
    "source": "gateway",
    "source_id": "ORDER_123",
}

headers = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
}

r = requests.post(URL, headers=headers, json=payload, timeout=120)

print(f"HTTP {r.status_code}")
print("-" * 60)

try:
    print(json.dumps(r.json(), indent=2, ensure_ascii=False))
except ValueError:
    print(r.text)