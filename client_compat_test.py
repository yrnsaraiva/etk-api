"""Prova de compatibilidade: usa o codigo do cliente runwithbroto SEM ALTERACOES,
apenas trocando ETK_BASE. As funcoes abaixo sao copiadas do repositorio."""
import re, sys, requests
from datetime import datetime
from types import SimpleNamespace

ETK_BASE = "http://127.0.0.1:8901"      # <-- unica alteracao
ETK_TOKEN = sys.argv[1]
EVENT_ID = sys.argv[2]
TIMEOUT = 30

# ---------- copiado de apps/events/views.py ----------
def _etk_request(method, path, json=None, timeout=TIMEOUT):
    url = f"{ETK_BASE}{path}"
    headers = {"Authorization": f"Bearer {ETK_TOKEN}"}
    if json is not None:
        headers["Content-Type"] = "application/json"
    resp = requests.request(method, url, headers=headers, json=json, timeout=timeout)
    resp.raise_for_status()
    return resp.json()

def _get_events_from_api():
    data = _etk_request("GET", "/back/borrow/external/events")
    return data.get("data", [])

def _get_event_from_api(event_id):
    data = _etk_request("GET", f"/back/borrow/external/events/{event_id}")
    return data.get("data", {})

def _create_ticket_in_api(payload):
    return _etk_request("POST", "/back/borrow/external/tickets", json=payload)

def _parse_datetime(value):
    if not value: return None
    if value.endswith('Z'): value = value[:-1] + '+00:00'
    return datetime.fromisoformat(value)

def _build_event_context(event_data):
    start_at = _parse_datetime(event_data.get("date"))
    prices = event_data.get("prices", [])
    active_price = next((p for p in prices if (p.get("status") or "").lower() == "active"),
                        prices[0] if prices else None)
    amount = active_price.get("amount") if active_price else None
    return {
        "id": event_data.get("id"),
        "title": event_data.get("name"),
        "city": (event_data.get("location") or {}).get("province") or "",
        "meeting_point": (event_data.get("location") or {}).get("details") or "",
        "start_at": start_at,
        "ticket_price_display": f"{amount} MTn" if amount is not None else "—",
        "priceId": active_price.get("id") if active_price else None,
        "confirmed_count": event_data.get("totalTicketsPurchased") or 0,
    }
# ---------- fim da copia ----------

print("1. event_list      :", [e["name"] for e in _get_events_from_api()])

ev = _build_event_context(_get_event_from_api(EVENT_ID))
print("2. event_detail    :", ev["title"], "|", ev["city"], "/", ev["meeting_point"],
      "|", ev["ticket_price_display"], "| priceId", ev["priceId"])
assert ev["priceId"], "cliente nao encontrou preco activo"

payload = {"priceId": ev["priceId"], "eventId": EVENT_ID, "phone": "258841234567",
           "fullName": "Maria Sitoe", "email": "maria@example.com", "paymentMethod": "mpesa"}
resp = _create_ticket_in_api(payload)

# a verificacao exacta que o cliente faz
assert resp.get("status") == "success" and resp.get("message") == "Ticket created successfully", resp
t = resp["data"]
print("3. ticket criado   :", t["id"], "| payment:", t["payment"], "| entered:", t["entered"])

# _save_ticket_locally le estes campos:
for campo in ("eventId", "priceId", "price", "entered", "status", "payment", "createdAt", "updatedAt"):
    assert campo in t, f"campo em falta: {campo}"
assert "amount" in t["price"]
print("4. campos que _save_ticket_locally espera: todos presentes")

# --- o que faltava na API original: sondar o pagamento ---
poll = _etk_request("GET", f"/back/borrow/external/tickets/{t['id']}")
print("5. polling         :", poll["data"]["payment"])

r = requests.post(f"{ETK_BASE}/back/payments/callback",
                  json={"ticketId": t["id"], "status": "succeeded",
                        "provider": "mpesa", "providerReference": "MP123"}, timeout=30)
print("6. callback        :", r.json()["data"]["payment"])
poll = _etk_request("GET", f"/back/borrow/external/tickets/{t['id']}")
print("7. polling apos pag:", poll["data"]["payment"])
assert poll["data"]["payment"] == "paid"

qr = poll["data"]["qrValue"]
ci = _etk_request("POST", "/back/borrow/external/tickets/check-in", json={"qrValue": qr})
print("8. check-in        :", ci["data"]["result"], "|", ci["message"])
ci = _etk_request("POST", "/back/borrow/external/tickets/check-in", json={"qrValue": qr})
print("9. check-in repetido:", ci["data"]["result"], "|", ci["message"])
assert ci["data"]["result"] == "already_entered"

ci = _etk_request("POST", "/back/borrow/external/tickets/check-in", json={"qrValue": "RWB|falso"})
print("10. QR forjado     :", ci["data"]["result"])

# esgotar o lote (restava 1)
_create_ticket_in_api({"priceId": ev["priceId"], "eventId": EVENT_ID, "phone": "258849999999"})
try:
    _create_ticket_in_api({"priceId": ev["priceId"], "eventId": EVENT_ID, "phone": "258848888888"})
    print("11. FALHA: vendeu a mais!")
except requests.HTTPError as e:
    print("11. esgotado       :", e.response.status_code, e.response.json()["message"])

try:
    _etk_request("GET", "/back/borrow/external/events")
except requests.HTTPError as e:
    pass
old = ETK_TOKEN
r = requests.get(f"{ETK_BASE}/back/borrow/external/events",
                 headers={"Authorization": "Bearer etk_live_chaveinventada"}, timeout=30)
print("12. chave invalida :", r.status_code, r.json()["message"])

print("\nCLIENTE runwithbroto COMPATIVEL — so mudou ETK_BASE")
