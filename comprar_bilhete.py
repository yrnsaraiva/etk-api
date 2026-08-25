#!/usr/bin/env python3
"""Testa o fluxo de compra de um bilhete, ponta a ponta, contra a API externa.

Uso:
    python comprar_bilhete.py
# Pa$$w0rd!123
Ajusta as constantes abaixo antes de correr. Precisas de:
  - BASE_URL apontando para o teu servidor (dev local ou staging)
  - API_KEY de um parceiro válido (ApiKeyAuthentication)
  - EVENT_ID e PRICE_ID de um evento publicado desse parceiro

O script:
  1. Lista os eventos do parceiro (para confirmar que a chave funciona)
  2. Cria o bilhete (POST /tickets) — isto já dispara start_payment()
  3. Se ficar `pending`, faz polling em GET /tickets/{id} até confirmar,
     falhar, ou esgotar o tempo — útil quando estás a usar o FakeProvider
     e ainda não simulaste o webhook manualmente.
"""

import sys
import time

import requests

# ----------------------------------------------------------------- config
BASE_URL = "http://localhost:8000/"
API_KEY = "etk_live_pi1-XXmO1l13wmS8yuV7Ynum"
EVENT_ID = "EVNT17876714449383"
PRICE_ID = "PRC17876714442303"
PHONE = "258849651834"
PAYMENT_METHOD = "mpesa"           # mpesa | emola | mkesh | card
POLL_SECONDS = 3
POLL_TIMEOUT = 60
# ---------------------------------------------------------------------

session = requests.Session()
session.headers.update({
    "Authorization": f"Bearer {API_KEY}",   # ajusta ao esquema real do ApiKeyAuthentication
    "Content-Type": "application/json",
})


def listar_eventos():
    resp = session.get(f"{BASE_URL}/back/borrow/external/events")
    resp.raise_for_status()
    body = resp.json()
    print(f"[1/3] {len(body.get('data', []))} evento(s) visível(is) para esta chave.")
    return body


def criar_bilhete():
    payload = {
        "priceId": PRICE_ID,
        "eventId": EVENT_ID,
        "phone": PHONE,
        "paymentMethod": PAYMENT_METHOD,
        "fullName": "Cliente de Teste",
        "email": "teste@example.com",
    }
    resp = session.post(f"{BASE_URL}/back/borrow/external/tickets", json=payload)
    print(f"[2/3] POST /tickets -> {resp.status_code}")
    try:
        body = resp.json()
    except ValueError:
        print(resp.text)
        resp.raise_for_status()
        return None

    if resp.status_code >= 400:
        print("Erro:", body)
        resp.raise_for_status()

    ticket = body["data"]
    print(f"      bilhete {ticket['id']} criado, payment={ticket['payment']}")
    if ticket.get("checkoutUrl"):
        print(f"      checkoutUrl: {ticket['checkoutUrl']}")
    if body.get("data", {}).get("paymentInstructions"):
        print(f"      instruções: {ticket.get('paymentInstructions')}")
    return ticket


def esperar_confirmacao(ticket_id: str):
    print(f"[3/3] a sondar GET /tickets/{ticket_id} (até {POLL_TIMEOUT}s)...")
    deadline = time.time() + POLL_TIMEOUT
    while time.time() < deadline:
        resp = session.get(f"{BASE_URL}/back/borrow/external/tickets/{ticket_id}")
        resp.raise_for_status()
        ticket = resp.json()["data"]
        payment = ticket["payment"]
        print(f"      payment={payment}")
        if payment == "paid":
            print("      ✅ pago.")
            return ticket
        if payment == "failed":
            print("      ❌ falhou.")
            return ticket
        time.sleep(POLL_SECONDS)
    print("      ⏱ tempo esgotado, ainda pending — confirma o webhook/reconciliação.")
    return None


if __name__ == "__main__":
    try:
        listar_eventos()
        ticket = criar_bilhete()
        if ticket and ticket["payment"] == "pending":
            esperar_confirmacao(ticket["id"])
    except requests.HTTPError as exc:
        print(f"Falhou: {exc}", file=sys.stderr)
        sys.exit(1)
