# Pagamentos (iMali.Way)

O gateway é a **iMali.Way** (Paytek). Substituiu por completo a Debito Pay:
não há código, rota nem configuração da Debito Pay. Os bilhetes antigos
mantêm `provider = "debitopay"` como histórico e nunca são tocados.

## O contrato

Fonte: *iMali Payment Gateway API — Partner Integration*, v1.4a (22-04-2026), e
as *Partner Specifications* (dados do ambiente de teste).

URL base: `https://paytek-africa.net:11901/api/partners/imaliway/v2`. A
documentação não lista um endereço separado para a sandbox: só mudam as
credenciais (`IMALIWAY_SANDBOX_*`).

**Autenticação.** Cada pedido leva `Authorization: Bearer <privateKey>` e
`X-Client-ID`. A `privateKey` é a `api_key` cifrada com a `publicKey` da Paytek
(RSA, padding PKCS#1 v1.5), em base64. A cifra é aleatória, por isso gera-se
uma chave nova por pedido (`imaliway.private_key()`).

> **A confirmar na sandbox:** a documentação mostra o formato da `privateKey`
> numa imagem. Assumimos base64 (o habitual). Se a API responder
> `500 Invalid token` com credenciais certas, é aqui que está o problema.

**Push payment** — `POST /payments`:

| campo | valor |
|---|---|
| `client_account_number` | telemóvel de 9 dígitos, sem 258 (`841234567`) |
| `amount` | mínimo 10 MT |
| `store_account_number` | conta STORE iMali (9 dígitos) |
| `partner_transaction_id` | exatamente 12 caracteres alfanuméricos |
| `payment_method` | `mpesa` · `emola` · `mkesh` · `imali` |
| `payment_type` / `transaction_type` | `push` / `C2B` |
| `expiration_datetime` | `Y-m-d H:i:s` (opcional; por omissão 2 minutos) |

O estado inicial é sempre `PENDING`: o cliente aprova no telemóvel. **Não há
confirmação síncrona** (a Debito Pay tinha-a para o M-Pesa).

**Estado** — `GET /payments/status?payment_type=push&partner_transaction_id=…`
devolve `PENDING | SUCCESS | FAILED | EXPIRED` e o tempo restante, **sem o
valor**. `EXPIRED` conta como falhado: a vaga volta.

**Webhook** — eventos `PAYMENT.PENDING | PAYMENT.SUCCESS | PAYMENT.FAILED`.
Assinatura: HMAC-SHA256 de `"{timestamp}.{corpo cru}"` com o segredo do
webhook; janela anti-replay de 5 minutos; reenvios até 10 vezes em 72 horas
se a resposta não for 2xx. A documentação é inconsistente nos cabeçalhos
(`X-Webhook-Signature`/`X-Webhook-Timestamp` nos exemplos,
`X-Signature`/`X-Timestamp` na tabela) e o exemplo traz o prefixo `sha256=`:
aceitamos as duas grafias, com ou sem prefixo.

**Não existe**: cartão, reembolso nem estorno automáticos. Só há
transferências B2C (saída de dinheiro), que não usamos.

## Configuração

Tudo por variáveis de ambiente — **nunca no repositório**:

| variável | o quê |
|---|---|
| `IMALIWAY_API_KEY` | `api_key` do parceiro |
| `IMALIWAY_CLIENT_ID` | `client_id` (cabeçalho `X-Client-ID`) |
| `IMALIWAY_PUBLIC_KEY` | `publicKey` em PEM (aceita `\n` literais, numa só linha) |
| `IMALIWAY_STORE_ACCOUNT_NUMBER` | conta STORE iMali |
| `IMALIWAY_WEBHOOK_SECRET` | `webhook_secret` |
| `IMALIWAY_BASE_URL` | opcional; por omissão o URL acima |
| `IMALIWAY_DEFAULT_METHOD` | opcional; `mpesa` |
| `IMALIWAY_PUSH_EXPIRATION_MINUTES` | opcional; `5` (nunca passa da reserva do bilhete) |
| `IMALIWAY_SANDBOX_*` | as mesmas, para chaves `etk_test_…` |

Duas coisas fora do código, combinadas com a Paytek:

1. **Whitelist de IP.** A Paytek só aceita pedidos de um IP registado. É o IP
   de saída do servidor da API (no Railway, é preciso um IP de saída fixo).
   Enquanto não estiver registado, a ligação é cortada.
2. **URL do webhook.** Dar à Paytek `https://<API>/back/payments/webhooks/imaliway`.

## Arquitetura

Um único módulo, sem abstração de "provider plugável" — só há um gateway:

```
ticketing/views.py  ──>  payments/services.py  ──>  payments/imaliway.py
                          (start_payment, _settle,
                           handle_webhook, reconcile_pending)
```

`payments/services.py` também chama de volta para `ticketing/services.py`
(`confirm_payment`, `reclaim_and_confirm`, `release`) — é lá que vivem as
regras sobre vagas e estado do bilhete; `payments/` só orquestra o gateway.

O `partner_transaction_id` (que guardamos em `Ticket.provider_charge_id`) é
derivado do id do bilhete, de forma determinística. Repetir o pedido para o
mesmo bilhete nunca cria uma segunda cobrança: o gateway responde 406 ("já em
uso") e o adaptador devolve o estado da cobrança que já existe.

## Fluxo

```
POST /tickets
  → create_ticket()     reserva a vaga
  → start_payment()     cria o push; o cliente aprova no telemóvel
                        bilhete "pending" (sempre assíncrono)

webhook PAYMENT.SUCCESS  -> assinatura -> valor -> paid -> enfileira aviso ao parceiro
webhook perdido          -> reconcile_payments (cron) sonda e confirma
webhook PAYMENT.FAILED   -> vaga libertada
estado EXPIRED           -> vaga libertada (na reconciliação)
sem pagamento em 15 min  -> expire_tickets liberta a vaga
pagamento tardio (depois
  dos 15 min)            -> tenta reservar de novo; sem vaga, fica "review"
```

Falhas ao criar a cobrança (valor abaixo de 10 MT, método não suportado, saldo
insuficiente, gateway em baixo…) libertam a vaga na hora e devolvem 402 (recusa)
ou 502 (indisponível).

## Defesas no caminho do dinheiro

**Assinatura.** Webhook sem HMAC válido, ou fora da janela de 5 minutos,
devolve 401 e não toca na base de dados. Comparação com `compare_digest`.

**Idempotência.** Cada evento é gravado em `ProviderEvent` com
`unique(provider, event_id)`. O gateway reenvia; o reenvio é ignorado.

**Valor.** Antes de marcar `paid`, compara-se o valor do webhook com
`ticket.amount`; se divergir, fica `review`. A consulta de estado não traz o
valor, por isso, na reconciliação, a confirmação confia no valor que nós
próprios pedimos.

**Pagamento tardio nunca é dinheiro perdido.** `reclaim_and_confirm` tenta
reservar de novo; sem vaga, fica `review` para decisão manual no `/admin/`.

**Só cobranças.** Eventos de transferência B2C ou de tipo desconhecido (por
exemplo, um futuro reembolso) não alteram bilhetes.

**Reconciliação.** O push é sempre assíncrono, por isso a reconciliação cobre
todos os métodos. Só sonda bilhetes do próprio gateway (`provider = imaliway`).

```bash
*/3 * * * * cd /app && python manage.py reconcile_payments
```

## Reembolsos

A iMali.Way não tem reembolso nem estorno automáticos, e não envia esse tipo
de evento. O reembolso é feito **à mão, fora do sistema** (transferência para o
cliente), e depois marcado no `/admin/` com "Marcar para reembolso": anula o
bilhete (`payment = refunded`, `status = cancelled`) e devolve a vaga se o
evento ainda não passou. `check_in` recusa bilhetes `refunded` e `cancelled`.

## Aviso ao parceiro: fila, não pedido síncrono

`notify_partner` não faz nenhum pedido HTTP — só cria uma linha em
`PartnerDelivery`, dentro da mesma transação que confirma o pagamento ou o
reembolso. Isto acontece assim porque um parceiro lento não pode prender um
worker do webhook da iMali.Way, e um aviso não pode perder-se só porque o
parceiro estava em baixo no momento exato da confirmação.

A entrega de facto corre à parte, no comando `deliver_webhooks` (cron, a
cada minuto): espera crescente entre tentativas (1, 5, 15, 60 min) e
desiste ao fim de um dia. `X-ETK-Delivery-ID` é o id da linha de
`PartnerDelivery`, não o id do bilhete — um bilhete pago e depois
reembolsado gera duas entregas, cada uma com o seu próprio id, para o
parceiro filtrar repetidos.

```bash
* * * * * cd /app && python manage.py deliver_webhooks
```

`webhook_secret` é gerado automaticamente (`secrets.token_urlsafe(32)`)
assim que um `User` tem `webhook_url` e ainda não tem segredo — nunca
existe a combinação insegura de um webhook sem segredo.

## Chaves de teste (sandbox)

Uma `ApiKey` com `environment=test` (prefixo `etk_test_…`) nunca cobra
dinheiro real: `Ticket.test_mode` fica `True` na criação (lido de
`request.auth.environment`, a chave que autenticou o pedido) e
`start_payment`/`reconcile_pending` passam `sandbox=True` a
`payments/imaliway.py`, que troca `settings.IMALIWAY` por
`settings.IMALIWAY_SANDBOX` — credenciais, conta STORE e segredo do webhook
são os do ambiente de teste da Paytek.

O webhook é um único endpoint para as duas contas: `parse_webhook` tenta a
assinatura com o segredo live e, se não bater, com o da sandbox — por isso
`IMALIWAY_SANDBOX_WEBHOOK_SECRET` também tem de estar configurado para os
eventos da sandbox serem aceites.

## Testar

```bash
python manage.py test                                          # suite completa
python manage.py test payments                                 # pagamento, sem rede (mocks)
python manage.py test_concurrency --vagas 3 --compradores 10   # exige PostgreSQL
```

`payments/tests.py` cobre, sempre com `unittest.mock` em
`payments.imaliway.requests.request` (nunca toca na rede):

- **o pedido:** o payload do contrato, o número sem indicativo, a
  `privateKey` (decifrada com a chave privada de teste, tem lá a `api_key`),
  a expiração do push, o valor mínimo, o método não suportado (nunca cai em
  M-Pesa por engano), a repetição que não duplica a cobrança (406);
- **os erros:** recusa do gateway (402), gateway em baixo (502), token inválido
  (configuração, não recusa), configuração em falta;
- **o webhook:** assinatura (com/sem prefixo, os dois nomes de cabeçalho, sem
  timestamp), anti-replay, idempotência, valor adulterado, falhado, tardio com
  e sem vaga, transferência B2C e tipos desconhecidos ignorados, bilhetes de
  outro gateway intocados, segredo da sandbox;
- **a reconciliação** e as **chaves de teste** (credenciais da sandbox).

## Variáveis de ambiente

```
IMALIWAY_API_KEY=...
IMALIWAY_CLIENT_ID=...
IMALIWAY_PUBLIC_KEY="-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----"
IMALIWAY_STORE_ACCOUNT_NUMBER=...
IMALIWAY_WEBHOOK_SECRET=...
PUBLIC_BASE_URL=https://a-sua-api.com

# sandbox (chaves etk_test_…) — por omissão herda BASE_URL e DEFAULT_METHOD da conta live
IMALIWAY_SANDBOX_API_KEY=...
IMALIWAY_SANDBOX_CLIENT_ID=...
IMALIWAY_SANDBOX_PUBLIC_KEY="-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----"
IMALIWAY_SANDBOX_STORE_ACCOUNT_NUMBER=...
IMALIWAY_SANDBOX_WEBHOOK_SECRET=...
```

Um valor em falta falha cedo, antes de chegar à rede, com uma mensagem que
diz qual variável falta.
