# Estrutura da etk-api

## Árvore

```
etk_api/
├── manage.py
├── requirements.txt
├── Procfile                              release: migrate · web: gunicorn
├── .env.example                          modelo das variáveis de ambiente
├── .gitignore                            db.sqlite3, .env, __pycache__
│
├── config/                               ── configuração do projeto
│   ├── settings.py               91      apps, BD, DRF, gateway, segurança
│   ├── urls.py                   40      todas as rotas, num sítio só
│   ├── envelope.py               36      {status, message, data} + exception handler
│   └── wsgi.py                    6
│
├── partners/                             ── QUEM VENDE
│   ├── models.py                 78      User (organizador) · ApiKey (hash SHA-256)
│   ├── authentication.py         30      lê "Bearer etk_live_…"
│   ├── admin.py                  20
│   └── migrations/
│
├── catalog/                              ── O QUE SE VENDE
│   ├── models.py                133      Event · Price (lote com stock)
│   ├── views.py                 110      CRUD do organizador (JWT)
│   ├── admin.py                  17
│   └── migrations/
│
├── ticketing/                            ── O QUE FOI VENDIDO
│   ├── models.py                117      Ticket · PaymentAttempt · CheckInLog
│   ├── services.py              173  ★  reserva, expiração, check-in
│   ├── views.py                 169      os 5 endpoints externos
│   ├── webhooks.py               40      notifica o parceiro
│   ├── admin.py                  23
│   └── migrations/
│
├── payments/                             ── COMO ENTRA O DINHEIRO
│   ├── models.py                 29      ProviderEvent (idempotência)
│   ├── services.py              146  ★  iniciar · aplicar webhook · reconciliar
│   ├── views.py                  33      endpoint do webhook
│   ├── providers/
│   │   ├── base.py               61      a interface (3 métodos, 2 dataclasses)
│   │   ├── fake.py               75      gateway falso, para testes
│   │   ├── debitopay.py         149      o real — CONTRATO-1..6 por confirmar
│   │   └── registry.py           17      resolve o provider pelo settings
│   ├── management/commands/
│   │   ├── reconcile_payments.py 11      cron */3 · webhooks perdidos
│   │   └── expire_tickets.py     11      cron */2 · vagas presas
│   └── migrations/
│
├── seed.py                       19      organizador + evento + chave de teste
├── client_compat_test.py        117      corre o cliente runwithbroto tal e qual
├── payments_test.py              86      10 verificações do fluxo de pagamento
├── concurrency_test.py           85      10 threads, 3 vagas → tem de dar 3
│
├── README.md                            contrato, endpoints, decisões
├── GUIA-DESENVOLVIMENTO.md              como construir isto do zero, 12 etapas
└── GUIA.md                              como pôr em produção, 12 passos
```

★ Os dois ficheiros onde vive a lógica que não é óbvia. Tudo o resto é
encanamento.

---

## Rotas

### Externas — os sites parceiros, com `Bearer etk_live_…`

| Método | Rota | Faz |
|---|---|---|
| GET | `/back/borrow/external/events` | lista eventos publicados |
| GET | `/back/borrow/external/events/{eventId}` | detalhe com preços |
| POST | `/back/borrow/external/tickets` | **cria bilhete + reserva vaga + cobra** |
| GET | `/back/borrow/external/tickets/{ticketId}` | sonda o estado do pagamento |
| POST | `/back/borrow/external/tickets/check-in` | valida o QR à entrada |

### Gateway — sem autenticação, protegidas por assinatura

| Método | Rota | Faz |
|---|---|---|
| POST | `/back/payments/webhooks/debitopay` | confirma pagamento (HMAC + idempotente) |
| POST | `/back/payments/callback` | callback genérico, para testes |

### Gestão — o organizador, com JWT

| Método | Rota | Faz |
|---|---|---|
| POST | `/api/auth/token/` | obtém access + refresh |
| — | `/api/events/` | CRUD de eventos |
| GET | `/api/events/{id}/tickets/` | participantes e contagens |
| — | `/api/prices/` | CRUD de lotes |
| POST | `/api/api-keys/` | emite chave (valor em claro só aqui) |
| DELETE | `/api/api-keys/{id}/` | revoga sem apagar histórico |
| — | `/admin/` | painel Django |

---

## Modelos

```
User (partners)
 ├─ ApiKey            key_hash, last_four, revoked_at, last_used_at
 └─ Event (catalog)   id=EVNT…, name, date, province, status
     └─ Price         id=PRC…, amount, quantity_total, quantity_reserved
         └─ Ticket    id=TCKT…, phone, payment, entered, provider_charge_id
             ├─ PaymentAttempt
             └─ (CheckInLog e ProviderEvent ficam soltos, para auditoria)
```

A cadeia `Event → Price → Ticket` é o eixo do sistema. O `Price` é o lote com
stock — é aí que a contagem impede vender a mais, e por isso o `Ticket` aponta
ao `Price` e não diretamente ao `Event`.

---

## Fluxos

**Compra**

```
POST /tickets
  → create_ticket()          select_for_update no Price, reserva 1 vaga
  → start_payment()          cria a cobrança no gateway
  ← bilhete pending, 15 min para pagar

webhook succeeded
  → assinatura válida?       senão 401, não toca na BD
  → evento já visto?         ProviderEvent unique → ignora duplicado
  → valor bate certo?        senão retém para revisão manual
  → confirm_payment()        paid + avisa o parceiro

webhook perdido  → reconcile_payments (cron) sonda e confirma
webhook failed   → release() devolve a vaga
sem pagamento    → expire_tickets (cron) devolve a vaga
```

**Entrada**

```
POST /tickets/check-in  {"qrValue": "TCKT…|hmac"}
  → assinatura do QR válida?    senão invalid_qr
  → é deste organizador?        senão not_found
  → payment == paid?            senão not_paid
  → já entrou?                  senão already_entered
  → marca entered, grava CheckInLog
```

---

## Onde mexer para

| Quero… | Ficheiro |
|---|---|
| mudar o prazo da reserva | `config/settings.py` → `TICKET_RESERVATION_MINUTES` |
| trocar de gateway | `payments/providers/` → novo ficheiro + `PAYMENT_PROVIDERS` |
| acertar o contrato Debito Pay | `payments/providers/debitopay.py` → `CONTRATO-1..6` |
| mudar o formato dos IDs | `catalog/models.py` → `make_id()` |
| acrescentar campo à resposta | o `to_api()` do modelo respetivo |
| mudar regras de venda | `ticketing/services.py` → `create_ticket()` |
| mudar regras de entrada | `ticketing/services.py` → `check_in()` |
