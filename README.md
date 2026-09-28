# API de bilhetes

API REST de venda de bilhetes para eventos, em Django e Django REST
Framework. Sites parceiros vendem bilhetes através dela — pagamento pela
[Debito Pay](docs/pagamentos.md), check-in por QR assinado.

Replica o contrato da API da eTickets (`eticketsmz.site/back/borrow/external/...`)
que o site `runwithbroto` já consome, para esse cliente poder passar a usar
esta API mudando só a variável `ETK_BASE`. Por isso as rotas externas, o
envelope das respostas, os nomes em camelCase e o formato dos IDs são
impostos pelo cliente e não devem mudar.

| Documento | Para quê |
|---|---|
| `docs/pagamentos.md` | arquitetura do pagamento (Debito Pay, defesas, testes) |
| `GUIA.md` | pôr em produção (Railway, Postgres, cron) passo a passo |
| `GUIA-DESENVOLVIMENTO.md` | como o projeto foi construído, etapa a etapa |
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

O cliente verifica literalmente `message == "Ticket created successfully"`,
por isso essa string é parte do contrato.

## Endpoints

### Externos — consumidos pelo parceiro com `etk_live_...`

| Método | Rota |
|---|---|
| GET | `/back/borrow/external/events` |
| GET | `/back/borrow/external/events/{eventId}` |
| POST | `/back/borrow/external/tickets` |
| GET | `/back/borrow/external/tickets` — lista os bilhetes da chave |
| GET | `/back/borrow/external/tickets/{ticketId}` — sondar pagamento |
| POST | `/back/borrow/external/tickets/check-in` |

### Pagamento — sem autenticação, protegida por assinatura HMAC

| Método | Rota |
|---|---|
| POST | `/back/payments/webhooks/debitopay` |

### Gestão — o organizador, com JWT

`/api/auth/token/`, `/api/events/`, `/api/prices/`, `/api/api-keys/`,
`/api/events/{id}/tickets/` (dashboard), `/api/prices/{id}/invites/`
(convites — ver secção própria abaixo).

Estas rotas usam o mesmo envelope `{status, message, data}` das externas
(a paginação do DRF fica dentro de `data`) — exceto `/api/auth/token/…`,
que mantém o formato próprio do SimpleJWT (`{access, refresh}`).

## Correr localmente

```bash
pip install -r requirements.txt

export DEBUG=1                                   # HTTP local, cai em SQLite
export DJANGO_SECRET_KEY=qualquer-coisa-para-dev
export QR_SIGNING_KEY=qualquer-outra-coisa

python manage.py migrate
python manage.py seed_demo          # cria organizador, evento e chave
python manage.py runserver 8901
```

O `seed_demo` imprime o `EVENT_ID` e a `API_KEY` uma única vez — guarde-os.
Sem credenciais da Debito Pay, `POST /back/borrow/external/tickets` chega a
criar o bilhete `pending` mas a chamada ao gateway falha (sem `wallet_code`
configurada); ver `docs/pagamentos.md` para testar o pagamento a sério.

## Quatro coisas que esta versão corrige (em relação ao contrato original)

**1. `payment` desatualizado.** A API original não tem forma de reler um
bilhete. O `runwithbroto` grava `payment_status` no momento da criação —
quando ainda é `pending` — e o scanner compara com esse valor local. Quem
paga corretamente pode ficar à porta. Aqui há `GET /tickets/{id}` para
sondar e um aviso `ticket.paid` para o parceiro (ver `docs/pagamentos.md`).

**2. Overselling.** `create_ticket` usa `select_for_update()` na linha do
`Price`, incrementa com `F()` e tem um `CheckConstraint` como última rede.
Sem o lock, dois pedidos simultâneos leem "resta 1" e ambos passam —
`python manage.py test_concurrency` prova isto contra PostgreSQL.

**3. QR forjável.** O QR do `runwithbroto` é `RWB|<external_id>` — quem
souber o formato do ID entra sem bilhete. Aqui é `TCKT…|<hmac>`, assinado
com uma chave dedicada (`QR_SIGNING_KEY`) e verificado no servidor; um QR
sem assinatura é sempre recusado.

**4. Isolamento entre organizadores.** Bug real encontrado durante o
desenvolvimento: `GET /events` devolvia a agenda inteira da plataforma, e
`POST /tickets` não verificava que o `priceId` pertencia ao dono da chave
que estava a chamar. Uma chave do organizador A conseguia criar bilhete
contra o lote do organizador B. Corrigido em `ExternalEventListView`/
`ExternalEventDetailView` (filtro por `organizer=request.user`) e em
`create_ticket` (verificação antes de qualquer escrita). Testado em
`ticketing/tests.py`.

## Convites

O organizador pode emitir bilhetes gratuitos para patrocinadores, parceiros
ou imprensa, sem passar pelo gateway de pagamento.

```
POST /api/prices/{priceId}/invites/
{"quantity": 3, "holderName": "Patrocinador X", "note": "3 convites VIP"}
```

Um convite é um `Ticket` normal com `amount = 0` e `payment = "invited"` em
vez de `"paid"`. Ocupa vaga do lote da mesma forma que uma compra (mesmo
`select_for_update()`, mesma verificação de capacidade), e passa no
check-in tal como um bilhete pago (`Ticket.ENTRY_ALLOWED = {"paid", "invited"}`).

Duas diferenças deliberadas em relação à compra: não exige que o evento
esteja `PUBLISHED` (o organizador pode garantir lugares antes de abrir a
venda), e o dashboard (`/api/events/{id}/tickets/`) reporta `paid` e
`invited` em contadores separados, para convites não inflacionarem os
números de receita.

## Notas de produção

- **PostgreSQL.** O `select_for_update()` no SQLite é decorativo — só serve
  para desenvolvimento local (`DEBUG=1`).
- As chaves de API são guardadas em **hash SHA-256**; o valor em claro
  aparece uma única vez, na resposta ao `POST /api/api-keys/`.
- Três tarefas agendadas mantêm o sistema consistente — ver
  `GUIA.md` (Passo 8) e `docs/pagamentos.md`.
