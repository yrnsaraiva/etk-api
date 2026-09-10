# API de bilhetes

API replicada a partir do cliente em `yrnsaraiva/runwithbroto`, que consome
`https://eticketsmz.site/back/borrow/external/...`. O objetivo é que o site
parceiro funcione **mudando apenas `ETK_BASE`**.

Este README cobre o contrato e as decisões de arquitetura. Para o resto:

| Documento | Para quê |
|---|---|
| `GUIA-DESENVOLVIMENTO.md` | construir isto do zero, etapa a etapa |
| `GUIA.md` | pôr em produção (Railway, Postgres, cron) |
| `TESTES.md` | como correr e escrever testes |
| `GITHUB.md` | versionar sem vazar segredos |
| `ESTRUTURA.md` | mapa de ficheiros e rotas |

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
`/api/events/{id}/tickets/` (dashboard), `/api/prices/{id}/invites/`
(convites — ver secção própria abaixo).

## Correr e provar

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo          # cria organizador, evento e chave
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

**4. Isolamento entre organizadores.** Bug real encontrado durante o
desenvolvimento, não presente no contrato original mas introduzido por engano
na primeira versão desta API: `GET /events` devolvia a agenda inteira da
plataforma, e pior — `POST /tickets` não verificava que o `priceId` pertencia
ao dono da chave que estava a chamar. Uma chave do organizador A conseguia
criar bilhete contra o lote do organizador B, reservando o stock dele. Corrigido
em `ExternalEventListView`/`ExternalEventDetailView` (filtro por
`organizer=request.user`) e em `create_ticket` (verificação antes de qualquer
escrita). Testado em `ticketing/tests.py`, com um teste que prova a falha ao
reverter a correção antes de a confirmar.

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

## Convites

O organizador pode emitir bilhetes gratuitos para patrocinadores, parceiros
ou imprensa, sem passar pelo gateway de pagamento.

```
POST /api/prices/{priceId}/invites/
{"quantity": 3, "holderName": "Patrocinador X", "note": "3 convites VIP"}
```

Um convite é um `Ticket` normal com `amount = 0` e `payment = "invited"` em
vez de `"paid"` — reaproveita o campo que já existia para congelar o preço no
momento da emissão, sem precisar de um modelo novo. Ocupa vaga do lote da
mesma forma que uma compra (mesmo `select_for_update()`, mesma verificação de
capacidade), e passa no check-in tal como um bilhete pago
(`Ticket.ENTRY_ALLOWED = {"paid", "invited"}`).

Duas diferenças deliberadas em relação à compra: não exige que o evento
esteja `PUBLISHED` (o organizador pode garantir lugares antes de abrir a
venda), e o dashboard (`/api/events/{id}/tickets/`) reporta `paid` e
`invited` em contadores separados, para convites não inflacionarem os
números de receita.

Testes em `ticketing/tests_invites.py`: 15 casos, incluindo o mesmo
isolamento entre organizadores da secção anterior — a chave de um não
consegue convidar para o lote de outro.

---

# Pagamentos (Debito Pay)

## O contrato, confirmado pela documentação oficial

URL base: `https://gyqoaningqhurhvdugne.supabase.co/functions/v1`. Um único
ponto de entrada, `/payment-orchestrator`, que encaminha internamente
conforme `payment_method` (`mpesa`, `emola`, `mkesh`, `visa_mastercard`,
`payfast`). Autenticação por `Authorization: Bearer sk_live_...`.

Duas particularidades que não são o padrão de mercado e que moldaram o
adaptador:

**M-Pesa confirma de forma síncrona.** A própria resposta ao `POST` inicial
já vem com `status: "success"` — não há webhook a esperar. e-Mola, mKesh e
cartões continuam assíncronos (`status: "pending"`, confirmação por
`payment.completed` ou pelo `check-status`).

**Cada método de pagamento tem a sua própria carteira.** `wallet_code` não é
o mesmo para M-Pesa, e-Mola e cartão — é preciso configurar uma por método
(`DEBITOPAY_WALLET_MPESA`, `DEBITOPAY_WALLET_EMOLA`, etc.), além do
`merchant_id`, comum a todas.

Webhook assinado em `X-Webhook-Signature`, HMAC-SHA256 em hex sobre o corpo
cru — confirmado contra o exemplo Node.js da própria documentação.

## Arquitetura

```
ticketing/views.py  ──>  payments/services.py  ──>  providers/base.py (porta)
                                                     ├── debitopay.py  (produção)
                                                     └── fake.py       (testes)
```

`PAYMENT_PROVIDER=fake` corre o fluxo inteiro sem credenciais.

## Fluxo

```
POST /tickets
  → create_ticket()     reserva a vaga
  → start_payment()     cria a cobrança
       M-Pesa            já confirma aqui — bilhete sai "paid"
       e-Mola/mKesh      "pending", confirma por webhook
       cartão            "pending", checkout_url para o Hosted Checkout

webhook payment.completed  -> assinatura -> valor -> paid -> avisa parceiro
webhook perdido             -> reconcile_payments (cron 2-5 min) sonda e confirma
webhook payment.failed      -> vaga libertada
sem pagamento em 15 min     -> expire_tickets liberta a vaga
```

## Quatro defesas no caminho do dinheiro

**Assinatura.** Webhook sem HMAC válido devolve 401 e não toca na base de dados.
Comparação com `compare_digest`, para o tempo de resposta não revelar o segredo.

**Idempotência.** Cada evento é gravado em `ProviderEvent` com
`unique(provider, event_id)`. O gateway reenvia em caso de timeout; o reenvio é
ignorado em vez de confirmar o bilhete duas vezes.

**Valor.** Um webhook autêntico pode trazer um valor adulterado se o gateway
tiver sido enganado a montante. A mesma verificação corre também na
confirmação síncrona do M-Pesa — não é exclusiva do webhook. Antes de marcar
`paid`, compara-se o valor devolvido com `ticket.amount`; se divergir, o
bilhete fica retido para revisão manual em vez de confirmado.

**Reconciliação.** Cobre e-Mola, mKesh e cartão — métodos assíncronos cujo
webhook pode perder-se. M-Pesa raramente aparece aqui, porque já confirma na
chamada inicial.

```bash
*/3 * * * * cd /app && python manage.py reconcile_payments
```

## Testar

```bash
python manage.py test                            # suite completa: 81 testes
python manage.py test payments.tests_debitopay    # adaptador, sem rede (mocks)
PAYMENT_PROVIDER=fake python manage.py test_payment_flow   # fluxo completo
```

`tests_debitopay.py` cobre: o payload certo por método, a confirmação
síncrona do M-Pesa, a wallet certa por método, erro do gateway traduzido,
assinatura válida/inválida, e a prova de que a assinatura é sobre o corpo
cru — assinar o JSON reserializado falha, de propósito.

Ver `TESTES.md` para as três camadas de teste do projeto (suite automática,
comandos de fluxo, manual) e como escrever testes novos.

## Variáveis de ambiente

```
PAYMENT_PROVIDER=debitopay
DEBITOPAY_BASE_URL=https://gyqoaningqhurhvdugne.supabase.co/functions/v1
DEBITOPAY_SECRET_KEY=sk_live_...
DEBITOPAY_WEBHOOK_SECRET=...
DEBITOPAY_MERCHANT_ID=...
DEBITOPAY_WALLET_MPESA=...
DEBITOPAY_WALLET_EMOLA=...
DEBITOPAY_WALLET_MKESH=...
DEBITOPAY_WALLET_CARD=...
DEBITOPAY_WALLET_PAYFAST=...
PUBLIC_BASE_URL=https://a-sua-api.com
```

Configure só as carteiras dos métodos que vai mesmo usar.