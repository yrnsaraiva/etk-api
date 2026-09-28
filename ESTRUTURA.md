# Estrutura da etk-api

## Árvore

```
etk_api/
├── manage.py
├── requirements.txt                      versões exatas
├── Procfile                              release: migrate · web: gunicorn
├── .gitignore                            db.sqlite3, .env, __pycache__, .idea/
├── .github/workflows/tests.yml           CI: PostgreSQL 16, test + test_concurrency
│
├── config/                               ── configuração do projeto
│   ├── settings.py                       apps, BD, DRF, Debito Pay, segurança
│   ├── urls.py                           todas as rotas, num sítio só
│   ├── envelope.py                       {status, message, data} + exception handler
│   ├── tests.py                          prova que falta DJANGO_SECRET_KEY faz falhar o arranque
│   └── wsgi.py
│
├── partners/                             ── QUEM VENDE
│   ├── models.py                     ★   User (organizador) · ApiKey (hash SHA-256)
│   ├── authentication.py                 lê "Bearer etk_live_…"
│   ├── admin.py
│   ├── management/commands/
│   │   └── seed_organizer.py             cria só o organizador + chave
│   └── migrations/
│
├── catalog/                              ── O QUE SE VENDE
│   ├── models.py                         Event · Price (lote com stock)
│   ├── views.py                          CRUD do organizador (JWT)
│   ├── admin.py
│   ├── management/commands/
│   │   └── seed_demo.py                  organizador + evento + lote + chave
│   └── migrations/
│
├── ticketing/                            ── O QUE FOI VENDIDO
│   ├── models.py                     ★   Ticket · PaymentAttempt · CheckInLog · PartnerDelivery
│   ├── services.py                   ★   reserva, expiração, check-in, reembolso, reclamação tardia
│   ├── views.py                          os endpoints externos
│   ├── webhooks.py                   ★   enfileira e entrega os avisos ao parceiro
│   ├── admin.py                          ações "Confirmar pagamento" / "Marcar para reembolso"
│   ├── management/commands/
│   │   ├── test_concurrency.py           prova que não se vende a mais (exige PostgreSQL)
│   │   └── deliver_webhooks.py           cron * * * * * · entrega PartnerDelivery pendentes
│   └── migrations/
│
├── payments/                             ── COMO ENTRA O DINHEIRO
│   ├── models.py                         ProviderEvent (idempotência)
│   ├── services.py                   ★   iniciar · aplicar webhook · reconciliar
│   ├── debitopay.py                  ★   o adaptador Debito Pay — sem abstração de provider plugável
│   ├── exceptions.py                     PaymentError e subclasses
│   ├── views.py                          endpoint do webhook
│   ├── tests.py                          fluxo webhook/reconciliação, com mocks
│   ├── tests_debitopay.py                adaptador isolado, com mocks
│   ├── management/commands/
│   │   ├── reconcile_payments.py         cron */3 · webhooks perdidos
│   │   └── expire_tickets.py             cron */2 · vagas presas
│   └── migrations/
│
├── README.md                             contrato, endpoints, decisões
├── docs/pagamentos.md                    arquitetura do pagamento, a fundo
├── GUIA.md                               como pôr em produção, passo a passo
└── GUIA-DESENVOLVIMENTO.md               como o projeto foi construído, etapa a etapa
```

★ Os ficheiros onde vive a lógica que não é óbvia. Tudo o resto é
encanamento.

---

## Rotas

### Externas — os sites parceiros, com `Bearer etk_live_…`

| Método | Rota | Faz |
|---|---|---|
| GET | `/back/borrow/external/events` | lista eventos publicados |
| GET | `/back/borrow/external/events/{eventId}` | detalhe com preços |
| POST | `/back/borrow/external/tickets` | **cria bilhete + reserva vaga + cobra** |
| GET | `/back/borrow/external/tickets` | lista os bilhetes da chave |
| GET | `/back/borrow/external/tickets/{ticketId}` | sonda o estado do pagamento |
| POST | `/back/borrow/external/tickets/check-in` | valida o QR à entrada |

### Gateway — sem autenticação, protegida por assinatura HMAC

| Método | Rota | Faz |
|---|---|---|
| POST | `/back/payments/webhooks/debitopay` | confirma pagamento (HMAC + idempotente) |

Não existe nenhuma rota de confirmação sem assinatura — a única forma de um
bilhete passar a `paid` é este webhook, a confirmação síncrona do M-Pesa, a
reconciliação, ou uma ação manual no `/admin/`.

### Gestão — o organizador, com JWT

| Método | Rota | Faz |
|---|---|---|
| POST | `/api/auth/token/` | obtém access + refresh |
| — | `/api/events/` | CRUD de eventos |
| GET | `/api/events/{id}/tickets/` | participantes e contagens |
| — | `/api/prices/` | CRUD de lotes |
| POST | `/api/prices/{id}/invites/` | emite convites grátis |
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
         └─ Ticket    id=TCKT…, phone, payment, status, entered, provider_charge_id
             ├─ PaymentAttempt
             └─ PartnerDelivery     fila de avisos ao parceiro (paid/refunded)

(CheckInLog e ProviderEvent ficam soltos, sem FK, para auditoria)
```

A cadeia `Event → Price → Ticket` é o eixo do sistema. O `Price` é o lote
com stock — é aí que a contagem impede vender a mais, e por isso o `Ticket`
aponta ao `Price` e não diretamente ao `Event`.

`Ticket` tem dois estados independentes: `status` (`valid`, `cancelled`,
`expired`) diz se o bilhete em si vale; `payment` (`pending`, `paid`,
`failed`, `refunded`, `invited`, `review`) diz o que aconteceu ao dinheiro.
`review` existe para os casos em que nem o código deve decidir sozinho —
pagamento tardio sem vaga, ou valor/moeda divergente — e fica para decisão
manual no `/admin/`.

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
  → valor bate certo?        senão fica review (ver docs/pagamentos.md)
  → reserva ainda válida?    senão reclaim_and_confirm tenta reservar de novo
  → confirm_payment()        paid + enfileira aviso ao parceiro

webhook perdido    → reconcile_payments (cron) sonda e confirma
webhook failed     → release() devolve a vaga
webhook refunded/
       chargeback  → refund() anula o bilhete e devolve a vaga
sem pagamento      → expire_tickets (cron) devolve a vaga
```

**Entrada**

```
POST /tickets/check-in  {"qrValue": "TCKT…|hmac"}
  → assinatura do QR válida?    senão invalid_qr (sem assinatura também recusa)
  → é deste organizador?        senão not_found
  → payment in ENTRY_ALLOWED?   {"paid", "invited"} — senão not_paid
  → status == valid?            senão not_paid (bilhete cancelado/reembolsado)
  → já entrou?                  senão already_entered
  → marca entered, grava CheckInLog
```

---

## Onde mexer para

| Quero… | Ficheiro |
|---|---|
| mudar o prazo da reserva | `config/settings.py` → `TICKET_RESERVATION_MINUTES` |
| acertar o contrato Debito Pay | `payments/debitopay.py` (topo do ficheiro) |
| mudar o formato dos IDs | `catalog/models.py` → `make_id()` |
| acrescentar campo à resposta | o `to_api()` do modelo respetivo |
| mudar regras de venda | `ticketing/services.py` → `create_ticket()` |
| mudar regras de entrada | `ticketing/services.py` → `check_in()` |
| mudar a política de novas tentativas do aviso ao parceiro | `ticketing/webhooks.py` → `BACKOFF_MINUTES`, `GIVE_UP_AFTER` |
