# API de bilhetes — compatível com o contrato eTickets MZ

API replicada a partir do cliente em `yrnsaraiva/runwithbroto`, que consome
`https://eticketsmz.site/back/borrow/external/...`. O objetivo é que o site
parceiro funcione **mudando apenas `ETK_BASE`**.

## Contrato extraído do cliente

| Aspeto | Convenção |
|---|---|
| Autenticação | `Authorization: Bearer etk_live_...` (chave de parceiro, não JWT) |
| Envelope | `{"status": "success", "message": "...", "data": ...}` |
| Nomes de campos | camelCase (`imageUrl`, `priceId`, `fullName`, `totalTicketsPurchased`) |
| IDs | strings com prefixo — `EVNT<epoch><4 dígitos>`, `PRC…`, `TCKT…` |
| Datas | ISO 8601 com `Z` |
| Comprador | identificado por telefone `258XXXXXXXXX`; sem conta de utilizador |
| Pagamento | assíncrono (push no telemóvel); bilhete nasce `pending` |

O cliente verifica literalmente `message == "Ticket created successfully"`, por
isso essa string é parte do contrato.

## Endpoints

### Externos — consumidos pelo parceiro com `etk_live_...`
| Método | Rota |
|---|---|
| GET | `/back/borrow/external/events` |
| GET | `/back/borrow/external/events/{eventId}` |
| POST | `/back/borrow/external/tickets` |
| GET | `/back/borrow/external/tickets/{ticketId}` — **novo**, sondar pagamento |
| POST | `/back/borrow/external/tickets/check-in` — **novo** |
| POST | `/back/payments/callback` — gateway confirma o pagamento |

### Gestão — o organizador, com JWT
`/api/auth/token/`, `/api/events/`, `/api/prices/`, `/api/api-keys/`,
`/api/events/{id}/tickets/` (dashboard).

## Correr e provar

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py shell < seed.py          # cria organizador, evento e chave
python manage.py runserver 8901
python client_compat_test.py <API_KEY> <EVENT_ID>
```

`client_compat_test.py` copia `_etk_request`, `_get_events_from_api`,
`_get_event_from_api`, `_create_ticket_in_api` e `_build_event_context` do
runwithbroto **sem alterações** e corre-os contra esta API.

## Três coisas que esta versão corrige

**1. `payment` desatualizado.** A API original não tem forma de reler um bilhete.
O runwithbroto grava `payment_status` no momento da criação — quando ainda é
`pending` — e o scanner compara com esse valor local em
`apps/scanner/views.py:69`. Quem paga corretamente pode ficar à porta. Aqui há
`GET /tickets/{id}` para sondar e um webhook `ticket.paid` para o parceiro.

**2. Overselling.** `create_ticket` usa `select_for_update()` na linha do `Price`,
incrementa com `F()` e tem um `CheckConstraint` como última rede. Sem o lock,
dois pedidos simultâneos leem "resta 1" e ambos passam.

**3. QR forjável.** O QR do runwithbroto é `RWB|<external_id>` — quem souber o
formato do ID entra sem bilhete. Aqui é `TCKT…|<hmac>`, verificado no servidor.

## Notas de produção

- **PostgreSQL.** O `select_for_update()` no SQLite é decorativo.
- Agende `ticketing.services.expire_stale_tickets()` a cada minuto.
- As chaves de API são guardadas em **hash SHA-256**; o valor em claro aparece
  uma única vez, na resposta ao `POST /api/api-keys/`.
- `_etk_request` do cliente chama `raise_for_status()` antes de ler o corpo, por
  isso a mensagem de erro nunca chega ao utilizador. Correção no lado do cliente:

  ```python
  resp = requests.request(method, url, headers=headers, json=json, timeout=timeout)
  payload = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
  if not resp.ok:
      raise EtkError(payload.get("message") or f"HTTP {resp.status_code}")
  return payload
  ```

---

# Pagamentos (Debito Pay)

## Estado da documentação

`debitopay.com/api-docs` e `/developers/payments-api` são páginas de marketing:
anunciam referência REST, SDKs e sandbox, mas **não publicam endpoints, nomes de
campos nem esquema de assinatura**. A referência real deve estar atrás do login.

Por isso o gateway está isolado num adaptador. Tudo o que depende do contrato
real vive em `payments/providers/debitopay.py`, marcado como `CONTRATO-1..6`:

| # | O que confirmar na documentação real |
|---|---|
| 1 | `base_url` e caminho de criação de cobrança (assumi `POST /v1/charges`) |
| 2 | Cabeçalho de autenticação — `Bearer`? `X-API-Key`? par público/secreto? |
| 3 | Nomes dos campos no pedido; **amount em unidades ou cêntimos** |
| 4 | Nomes na resposta e lista de valores de estado |
| 5 | Cabeçalho e codificação da assinatura do webhook (hex ou base64) |
| 6 | Se a assinatura cobre o corpo cru ou o JSON re-serializado |

Confirmados esses seis pontos, nada fora deste ficheiro muda.

## Arquitetura

```
ticketing/views.py  ──>  payments/services.py  ──>  providers/base.py (porta)
                                                     ├── debitopay.py  (produção)
                                                     └── fake.py       (testes)
```

`PAYMENT_PROVIDER=fake` corre o fluxo inteiro sem credenciais.

## Fluxo

```
POST /tickets      -> bilhete pending + cobrança no gateway + vaga reservada
cliente confirma no telemóvel
webhook succeeded  -> verifica assinatura -> verifica valor -> paid -> avisa parceiro
webhook perdido    -> reconcile_payments (cron 2-5 min) sonda e confirma
webhook failed     -> vaga libertada
sem pagamento      -> expire_stale_tickets liberta a vaga
```

## Quatro defesas no caminho do dinheiro

**Assinatura.** Webhook sem HMAC válido devolve 401 e não toca na base de dados.
Comparação com `compare_digest`, para o tempo de resposta não revelar o segredo.

**Idempotência.** Cada evento é gravado em `ProviderEvent` com
`unique(provider, event_id)`. O gateway reenvia em caso de timeout; o reenvio é
ignorado em vez de confirmar o bilhete duas vezes.

**Valor.** Um webhook autêntico pode trazer um valor adulterado se o gateway
tiver sido enganado a montante. Antes de marcar `paid`, compara-se
`event.amount` com `ticket.price.amount` — se divergir, o bilhete fica retido
para revisão manual em vez de ser confirmado.

**Reconciliação.** Webhooks perdem-se, e em mobile money perdem-se com
frequência. `reconcile_pending()` sonda o gateway sobre cada bilhete pendente.
Sem isto, quem paga e cujo webhook se perde fica à porta com o dinheiro fora.

```bash
*/3 * * * * cd /app && python manage.py reconcile_payments
```

## Testar

```bash
PAYMENT_PROVIDER=fake python manage.py shell < payments_test.py
```

Cobre: criação da cobrança, webhook, reenvio duplicado, assinatura falsa,
valor adulterado, pagamento falhado a libertar a vaga, e webhook perdido
recuperado pela reconciliação.

## Variáveis de ambiente

```
PAYMENT_PROVIDER=debitopay
DEBITOPAY_BASE_URL=https://api.debitopay.com
DEBITOPAY_SECRET_KEY=...
DEBITOPAY_WEBHOOK_SECRET=...
DEBITOPAY_SIGNATURE_HEADER=X-Debito-Signature
PUBLIC_BASE_URL=https://a-sua-api.com
```
