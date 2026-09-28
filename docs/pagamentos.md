# Pagamentos (Debito Pay)

## O contrato, confirmado pela documentação oficial

URL base: `https://gyqoaningqhurhvdugne.supabase.co/functions/v1`. Um único
ponto de entrada, `/payment-orchestrator`, que encaminha internamente
conforme `payment_method` (`mpesa`, `emola`, `mkesh`, `visa_mastercard`,
`payfast`). Autenticação por `Authorization: Bearer sk_live_...` (ou
`sk_sandbox_...`).

Duas particularidades que não são o padrão de mercado e que moldaram o
adaptador (`payments/debitopay.py`):

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

> `payments/debitopay.py` avisa no topo que este contrato foi copiado de
> código anterior. Um teste real em sandbox antes de qualquer alteração
> grande evita construir sobre suposições.

## Arquitetura

Um único módulo, sem abstração de "provider plugável" — só há um gateway:

```
ticketing/views.py  ──>  payments/services.py  ──>  payments/debitopay.py
                          (start_payment, _settle,
                           handle_webhook, reconcile_pending)
```

`payments/services.py` também chama de volta para `ticketing/services.py`
(`confirm_payment`, `reclaim_and_confirm`, `refund`, `release`) — é lá que
vivem as regras sobre vagas e estado do bilhete; `payments/` só orquestra o
gateway.

## Fluxo

```
POST /tickets
  → create_ticket()     reserva a vaga
  → start_payment()     cria a cobrança
       M-Pesa            já confirma aqui — bilhete sai "paid"
       e-Mola/mKesh      "pending", confirma por webhook
       cartão            "pending", checkout_url para o Hosted Checkout

webhook payment.completed   -> assinatura -> valor -> paid -> enfileira aviso ao parceiro
webhook perdido             -> reconcile_payments (cron) sonda e confirma
webhook payment.failed      -> vaga libertada
webhook payment.refunded/
        payment.chargeback  -> anula o bilhete, devolve a vaga (ver "Reembolsos")
sem pagamento em 15 min     -> expire_tickets liberta a vaga
pagamento tardio (depois
  dos 15 min)               -> tenta reservar de novo; sem vaga, fica "review"
```

## Cinco defesas no caminho do dinheiro

**Assinatura.** Webhook sem HMAC válido devolve 401 e não toca na base de
dados. Comparação com `compare_digest`, para o tempo de resposta não
revelar o segredo.

**Idempotência.** Cada evento é gravado em `ProviderEvent` com
`unique(provider, event_id)`. O gateway reenvia em caso de timeout; o
reenvio é ignorado em vez de confirmar o bilhete duas vezes.

**Valor.** Um webhook autêntico pode trazer um valor adulterado se o
gateway tiver sido enganado a montante. A mesma verificação corre também na
confirmação síncrona do M-Pesa — não é exclusiva do webhook. Antes de
marcar `paid`, compara-se o valor devolvido com `ticket.amount`; se
divergir, o bilhete fica `review` em vez de confirmado (e o cron de
expiração já não lhe toca).

**Pagamento tardio nunca é dinheiro perdido.** Se o pagamento chega depois
de a reserva expirar (comum em mobile money), `reclaim_and_confirm` tenta
reservar de novo uma vaga no mesmo lote — confirma se houver, fica `review`
para decisão manual no `/admin/` se não houver. O webhook responde sempre
200 a eventos autênticos; o resultado fica em `ProviderEvent.outcome` e um
log `ERROR` sinaliza o bilhete.

**Reconciliação.** Cobre e-Mola, mKesh e cartão — métodos assíncronos cujo
webhook pode perder-se. M-Pesa raramente aparece aqui, porque já confirma
na chamada inicial.

```bash
*/3 * * * * cd /app && python manage.py reconcile_payments
```

## Reembolsos e chargebacks

`payment.refunded` e `payment.chargeback` (`ticketing.services.refund`)
anulam o bilhete: `payment = refunded`, `status = cancelled`, e a vaga
volta ao lote se o evento ainda não passou. `check_in` já recusa bilhetes
`refunded` (via `Ticket.ENTRY_ALLOWED`) e `cancelled` (via `status`).

Um bilhete `review` também pode ser marcado para reembolso a partir do
`/admin/` — nesse caso não havia vaga ocupada, por isso nada é devolvido ao
lote.

## Aviso ao parceiro: fila, não pedido síncrono

`notify_partner` não faz nenhum pedido HTTP — só cria uma linha em
`PartnerDelivery`, dentro da mesma transação que confirma o pagamento ou o
reembolso. Isto acontece assim porque um parceiro lento não pode prender um
worker do webhook da Debito Pay, e um aviso não pode perder-se só porque o
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
`payments/debitopay.py`, que troca `settings.DEBITOPAY` por
`settings.DEBITOPAY_SANDBOX` — URL, credenciais, `merchant_id` e carteiras
todos podem ser diferentes.

O webhook é um único endpoint para as duas contas: `parse_webhook` tenta a
assinatura com o segredo live e, se não bater, com o da sandbox — por isso
`DEBITOPAY_SANDBOX_WEBHOOK_SECRET` também tem de estar configurado para os
eventos da sandbox serem aceites.

## Testar

```bash
python manage.py test                            # suite completa
python manage.py test payments.tests_debitopay    # adaptador, sem rede (mocks)
python manage.py test payments                    # fluxo webhook/reconciliação, sem rede (mocks)
python manage.py test_concurrency --vagas 3 --compradores 10   # exige PostgreSQL
```

`tests_debitopay.py` cobre: o payload certo por método, a confirmação
síncrona do M-Pesa, a wallet certa por método, erro do gateway traduzido,
assinatura válida/inválida, e a prova de que a assinatura é sobre o corpo
cru — assinar o JSON reserializado falha, de propósito.

`payments/tests.py` cobre o fluxo de ponta a ponta via `APIClient` e
webhooks HTTP reais (com assinatura), incluindo os casos de pagamento
tardio, valor divergente e reembolso — tudo com `unittest.mock` em
`requests.post`, sem tocar na rede.

## Variáveis de ambiente

```
DEBITOPAY_BASE_URL=https://gyqoaningqhurhvdugne.supabase.co/functions/v1
DEBITOPAY_SECRET_KEY=sk_live_...
DEBITOPAY_WEBHOOK_SECRET=...
DEBITOPAY_MERCHANT_ID=...
DEBITOPAY_WALLET_MPESA=...
DEBITOPAY_WALLET_EMOLA=...
DEBITOPAY_WALLET_MKESH=...
DEBITOPAY_WALLET_CARD=...
DEBITOPAY_WALLET_PAYFAST=...
DEBITOPAY_DEFAULT_METHOD=mpesa
PUBLIC_BASE_URL=https://a-sua-api.com

# sandbox (chaves etk_test_…) — por omissão herda BASE_URL/DEFAULT_METHOD da conta live
DEBITOPAY_SANDBOX_SECRET_KEY=sk_sandbox_...
DEBITOPAY_SANDBOX_WEBHOOK_SECRET=...
DEBITOPAY_SANDBOX_MERCHANT_ID=...
DEBITOPAY_SANDBOX_WALLET_MPESA=...
DEBITOPAY_SANDBOX_WALLET_EMOLA=...
DEBITOPAY_SANDBOX_WALLET_MKESH=...
DEBITOPAY_SANDBOX_WALLET_CARD=...
DEBITOPAY_SANDBOX_WALLET_PAYFAST=...
```

Configure só as carteiras dos métodos que vai mesmo usar; um método sem
`wallet_code` falha cedo, antes de chegar à rede, com uma mensagem que diz
qual variável falta.
