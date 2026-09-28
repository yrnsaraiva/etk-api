# Guia de implementação, passo a passo

Do código na sua máquina até o runwithbroto a vender bilhetes contra a sua API.

Onze passos. Os passos 1 a 4 são locais e sem risco. O passo 5 é o único
irreversível se for feito à pressa. Não salte o passo 9.

---

## Passo 0 — Revogar o token exposto

**Antes de tudo o resto.** O `etk_live_…` está no
histórico público do `yrnsaraiva/runwithbroto`, em quatro ficheiros. Apagá-lo do
código não resolve: quem clonar o repo tira-o do histórico.

1. Entre no painel da eTickets e revogue essa chave.
2. Emita outra e guarde-a fora do código (passo 3).
3. Na mesma limpeza: a `SECRET_KEY` do Django está hardcoded e a `db.sqlite3`
   está commitada com o hash de password do `shakes` e dados de 2 compradores.
   Mude a password do admin e remova a base de dados do repositório:

```bash
cd runwithbroto
git rm --cached db.sqlite3
echo -e "db.sqlite3\n.env\n__pycache__/" >> .gitignore
git commit -m "remove base de dados e segredos do repositorio"
```

Isto limpa o presente, não o passado. Para o histórico, o repositório teria de
ser reescrito (`git filter-repo`) ou tornado privado. Como já esteve público,
assuma que o token e o hash foram vistos — a revogação é o que conta.

---

## Passo 1 — Correr localmente

```bash
cd etk-api
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export DEBUG=1                                    # HTTP local, cai em SQLite
export DJANGO_SECRET_KEY=$(python -c "from django.core.management.utils import get_random_secret_key as g; print(g())")
export QR_SIGNING_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(32))")

python manage.py migrate
python manage.py seed_demo
python manage.py runserver 8901
```

O `seed_demo` imprime o `EVENT_ID` e a `API_KEY`. **Guarde-os** — a chave
não volta a aparecer. Sem credenciais da Debito Pay ainda (passo 5), a
criação de bilhete falha ao tentar cobrar — o resto da API (listar
eventos, emitir convites, check-in) já funciona.

---

## Passo 2 — Provar que a proteção contra overselling funciona

Isto exige PostgreSQL (passo 4) — em SQLite o `select_for_update()` não
tranca nada, então corra este passo depois desse.

```bash
python manage.py test_concurrency --vagas 3 --compradores 10
```

Lança 10 "compradores" simultâneos sobre 3 vagas e falha
(`CommandError`) se saírem mais de 3 bilhetes. Deve terminar em
`OK — exatamente 3 bilhetes, nem mais nem menos.`

Depois, a suite automática:

```bash
python manage.py test
```

66+ testes, incluindo o fluxo de pagamento com mocks (`payments/tests.py`,
`payments/tests_debitopay.py` — nenhum toca na rede).

---

## Passo 3 — Segredos em variáveis de ambiente

`DJANGO_SECRET_KEY` e `QR_SIGNING_KEY` já foram geradas no passo 1 — para
produção, gere-as de novo (não reaproveite as de desenvolvimento) e guarde
num `.env` ou no painel da plataforma de deploy, nunca no código. O
`.gitignore` já cobre `.env`; confirme antes do primeiro commit:

```bash
git status --short | grep -c "\.env$"   # tem de dar 0
```

O hook em `scripts/pre-commit` bloqueia commits com segredos óbvios (chaves
`etk_live_…`/`sk_live_…`, uma `SECRET_KEY` gerada automaticamente pelo
Django, chaves privadas). Ative-o:

```bash
git config core.hooksPath scripts
```

---

## Passo 4 — PostgreSQL

**Não é opcional em produção.** A proteção contra vender bilhetes a mais
assenta em `select_for_update()`, que no SQLite não tranca nada de útil.
Com SQLite, dois pagamentos simultâneos do último bilhete passam ambos —
é exatamente o que o passo 2 prova.

```bash
docker run -d --name etk-db -e POSTGRES_PASSWORD=dev \
  -e POSTGRES_DB=etk -p 5432:5432 postgres:16

export DATABASE_URL=postgresql://postgres:dev@localhost:5432/etk
unset DEBUG          # com DATABASE_URL definida, já não precisa do fallback SQLite
python manage.py migrate
python manage.py seed_demo --reset
```

Repita o passo 2 contra o Postgres antes de seguir.

---

## Passo 5 — Credenciais Debito Pay

Crie a conta, entre no dashboard e obtenha as chaves de **sandbox** (não as
de produção ainda). `payments/debitopay.py` documenta no topo o contrato
assumido — endpoint único, ação `process`/`check-status`, assinatura
HMAC-SHA256 sobre o corpo cru — e avisa que foi copiado de código anterior.
Confirme contra a documentação oficial antes de ir para produção.

Preencha no `.env`:

```
DEBITOPAY_BASE_URL=https://...
DEBITOPAY_SECRET_KEY=sk_sandbox_...
DEBITOPAY_WEBHOOK_SECRET=...
DEBITOPAY_MERCHANT_ID=...
DEBITOPAY_WALLET_MPESA=...              # e as outras carteiras que for usar
```

Ver `docs/pagamentos.md` para a lista completa de variáveis e a arquitetura
do adaptador.

---

## Passo 6 — Testar contra a sandbox

Com um túnel para o webhook chegar à sua máquina:

```bash
ngrok http 8901
export PUBLIC_BASE_URL=https://abc123.ngrok.io
```

Registe no dashboard Debito Pay o webhook:
`https://abc123.ngrok.io/back/payments/webhooks/debitopay`

Faça uma compra real de sandbox e confirme três coisas nos logs:

1. A cobrança foi criada (o bilhete tem `provider_charge_id`).
2. O webhook chegou **e passou na assinatura** — um 401 aqui é quase
   sempre `DEBITOPAY_WEBHOOK_SECRET` errada, ou a Debito Pay a assinar
   algo diferente do que `_signature_ok` espera (ver `docs/pagamentos.md`).
3. O bilhete passou a `paid`.

Se o webhook não chegar, force a reconciliação para confirmar que o outro
caminho funciona:

```bash
python manage.py reconcile_payments
```

---

## Passo 7 — Deploy

O seu runwithbroto já está em Railway, portanto o `Procfile` está no formato
certo. Em qualquer plataforma, o essencial é o mesmo:

```
release: python manage.py migrate --noinput
web: python manage.py collectstatic --noinput && gunicorn config.wsgi:application --log-file - --workers 2 --threads 4 --timeout 60 --worker-class gthread
```

Variáveis a definir no painel (todas as do `.env`, mais):

```
DEBUG=0
ALLOWED_HOSTS=api.seudominio.com
CSRF_TRUSTED_ORIGINS=https://api.seudominio.com
PUBLIC_BASE_URL=https://api.seudominio.com
```

Com `DEBUG=0` o Django liga HSTS, redireccionamento SSL e cookies seguros
automaticamente. Verifique antes de publicar:

```bash
DEBUG=0 python manage.py check --deploy   # tem de dar 0 issues
```

Depois do deploy:

```bash
python manage.py createsuperuser
```

(o `collectstatic` já corre a cada arranque do `web`, como parte do Procfile
acima — não é preciso correr à mão.)

---

## Passo 8 — Os três cron jobs

Sem estes, o sistema degrada-se silenciosamente.

```
*/3 * * * * cd /app && python manage.py reconcile_payments
*/2 * * * * cd /app && python manage.py expire_tickets
*  * * * * cd /app && python manage.py deliver_webhooks
```

O `reconcile_payments` apanha quem pagou e cujo webhook se perdeu. Em mobile
money isto acontece com frequência suficiente para importar: sem ele, essa
pessoa fica à porta com o dinheiro já fora da conta.

O `expire_tickets` liberta vagas de reservas não pagas. Sem ele, um evento
esgota com bilhetes que ninguém comprou.

O `deliver_webhooks` entrega à parte os avisos ao parceiro (`ticket.paid`,
`ticket.refunded`) que `notify_partner` só enfileira. Sem ele, os avisos
ficam para sempre em `PartnerDelivery` e o site do parceiro nunca sabe que
um bilhete foi pago ou reembolsado.

Em Railway, use um serviço `cron` separado apontando ao mesmo repositório —
um serviço por job, cada um com o seu próprio horário e o comando acima
como *Start Command*.

---

## Passo 9 — Ligar o runwithbroto

No painel do organizador, emita a chave de produção:

```bash
curl -X POST https://api.seudominio.com/api/api-keys/ \
  -H "Authorization: Bearer <jwt>" \
  -d '{"label": "site runwithbroto", "environment": "live"}'
```

A chave em claro vem **só nesta resposta**. Guarde-a na hora.

No runwithbroto, três alterações. A primeira é a que faz tudo apontar para si:

```python
# apps/events/views.py e apps/core/views.py
ETK_BASE = os.environ["ETK_BASE"]      # https://api.seudominio.com
ETK_TOKEN = os.environ["ETK_TOKEN"]    # a chave nova, nunca no código
```

A segunda faz a mensagem de erro chegar ao utilizador. Hoje o
`_etk_request` chama `raise_for_status()` antes de ler o corpo, por isso quem
tenta comprar um bilhete esgotado vê "Não foi possível processar o pagamento"
em vez de "Bilhetes esgotados":

```python
def _etk_request(method, path, json=None, timeout=TIMEOUT):
    url = f"{ETK_BASE}{path}"
    headers = {"Authorization": f"Bearer {ETK_TOKEN}"}
    if json is not None:
        headers["Content-Type"] = "application/json"
    resp = requests.request(method, url, headers=headers, json=json, timeout=timeout)
    payload = resp.json() if "application/json" in resp.headers.get("content-type", "") else {}
    if not resp.ok:
        raise EtkError(payload.get("message") or f"HTTP {resp.status_code}")
    return payload
```

A terceira corrige o scanner. Hoje `apps/scanner/views.py:69` compara com o
`payment_status` guardado no momento da criação — quando ainda era `pending` —
e recusa quem pagou. Passe a consultar o estado real:

```python
data = _etk_request("GET", f"/back/borrow/external/tickets/{external_id}")
if data["data"]["payment"] != "paid":
    return JsonResponse({"status": "not_paid", ...})
```

Melhor ainda: registe o webhook do parceiro (campo `webhook_url` no seu
utilizador organizador) e deixe a API avisar quando cada bilhete é pago ou
reembolsado. O aviso é entregue por um cron a correr a cada minuto (ver
Passo 8), por isso não chega no mesmo instante da confirmação — se o
scanner precisar de saber logo, continue a sondar `GET /tickets/{id}`.

---

## Passo 10 — Um evento a sério, em pequeno

Antes de anunciar, crie um evento real com **5 bilhetes** e venda-os a si mesmo
e a duas pessoas de confiança. Confirme, na ordem:

- [ ] O evento aparece na agenda do runwithbroto
- [ ] A compra abre o pedido de pagamento no telemóvel
- [ ] O bilhete passa a `paid` sem intervenção manual
- [ ] O PDF/QR chega ao comprador
- [ ] O scanner autoriza a entrada **na primeira vez**
- [ ] O scanner recusa **na segunda**
- [ ] Ao esgotar, a sexta compra é recusada com mensagem legível
- [ ] Um bilhete não pago liberta a vaga passados 15 minutos

O sexto e o sétimo pontos são os que falham nas plataformas de bilhetes reais,
sempre no dia do evento e sempre com fila à porta.

---

## Passo 11 — Antes de escalar

- Ative logs estruturados e vigie `valor divergente` e `assinatura inválida` —
  são os dois sinais de tentativa de fraude.
- Defina backups automáticos do Postgres. Bilhetes vendidos não se recuperam.
- Monitore a taxa de `reconcile_payments` que confirma pagamentos: se subir
  muito, os webhooks estão a perder-se e vale a pena falar com a Debito Pay.
- Rode as chaves de API dos parceiros periodicamente — `POST /api/api-keys/`
  emite, `DELETE /api/api-keys/{id}/` revoga sem apagar o histórico.
