# Guia de implementação, passo a passo

Do código na sua máquina até o runwithbroto a vender bilhetes contra a sua API.

Onze passos. Os passos 1 a 4 são locais e sem risco. O passo 5 é o único
irreversível se for feito à pressa. Não salte o passo 9.

---

## Passo 0 — Revogar o token exposto

**Antes de tudo o resto.** O `etk_live_3SZPEA7FNCZAdqjIaBIIbI7j` está no
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
python manage.py migrate
python manage.py shell < seed.py
```

O `seed.py` imprime duas linhas. **Guarde-as** — a chave não volta a aparecer:

```
EVENT_ID=EVNT17874803908179
API_KEY=etk_live_tbC-qdsqKWhQjhf5ZTbr2ELs
```

Arranque com o gateway falso, para não precisar de credenciais ainda:

```bash
PAYMENT_PROVIDER=fake python manage.py runserver 8901
```

---

## Passo 2 — Provar que o cliente é compatível

Noutro terminal, com os valores do passo anterior:

```bash
python client_compat_test.py etk_live_tbC-qdsq... EVNT178748039...
```

Devem sair 12 linhas e terminar em `CLIENTE runwithbroto COMPATIVEL`. Este
script corre as funções `_etk_request`, `_get_events_from_api`,
`_get_event_from_api`, `_create_ticket_in_api` e `_build_event_context`
copiadas do seu repositório sem alterações.

Se falhar aqui, pare e resolva — os passos seguintes assumem esta base.

Depois, o fluxo de pagamento:

```bash
PAYMENT_PROVIDER=fake python manage.py shell < payments_test.py
```

Dez verificações, incluindo webhook duplicado, assinatura falsa e valor
adulterado. Deve terminar em `TUDO OK`.

---

## Passo 3 — Segredos em variáveis de ambiente

```bash
cp .env .env
python -c "from django.core.management.utils import get_random_secret_key as g; print(g())"
```

Cole o resultado em `DJANGO_SECRET_KEY`. O `.env` já está no `.gitignore` —
confirme antes do primeiro commit:

```bash
git status --short | grep -c "\.env$"   # tem de dar 0
```

---

## Passo 4 — PostgreSQL

**Não é opcional.** A proteção contra vender bilhetes a mais assenta em
`select_for_update()`, que no SQLite não tranca nada de útil. Com SQLite em
produção, dois pagamentos simultâneos do último bilhete passam ambos.

```bash
docker run -d --name etk-db -e POSTGRES_PASSWORD=dev \
  -e POSTGRES_DB=etk -p 5432:5432 postgres:16

export DATABASE_URL=postgresql://postgres:dev@localhost:5432/etk
python manage.py migrate
python manage.py shell < seed.py
```

O `settings.py` usa `DATABASE_URL` quando existe e cai no SQLite quando não.

Repita o passo 2 contra o Postgres antes de seguir.

---

## Passo 5 — Credenciais Debito Pay e os seis pontos do contrato

Crie a conta, entre no dashboard e obtenha as chaves de **sandbox** (não as de
produção ainda). Depois abra `payments/providers/debitopay.py` e confirme os
seis pontos marcados `CONTRATO-1` a `CONTRATO-6`:

| # | O que confirmar | Onde corrigir |
|---|---|---|
| 1 | URL base e caminho da cobrança (assumi `POST /v1/charges`) | `create_charge` |
| 2 | Cabeçalho de autenticação — `Bearer`? `X-API-Key`? | `_headers` |
| 3 | Nomes dos campos e **se `amount` vai em unidades ou cêntimos** | `create_charge` |
| 4 | Nomes na resposta e valores de estado | `STATUS_MAP`, `_to_charge` |
| 5 | Cabeçalho e codificação da assinatura (hex ou base64) | `_signature_ok` |
| 6 | Se a assinatura cobre o corpo cru ou o JSON re-serializado | `_signature_ok` |

O ponto 3 é o que dá cobranças 100× erradas. O ponto 6 é o que faz toda a
validação falhar por causa de espaços em branco: se eles assinam o corpo cru e
você validar sobre `json.dumps(request.data)`, nunca bate certo. O código já
valida sobre `request.body` (cru) e aceita hex e base64 até confirmar.

Preencha no `.env`:

```
PAYMENT_PROVIDER=debitopay
DEBITOPAY_BASE_URL=https://...
DEBITOPAY_SECRET_KEY=...
DEBITOPAY_WEBHOOK_SECRET=...
```

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
2. O webhook chegou **e passou na assinatura** — se der 401, é o ponto 5 ou 6.
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
web: gunicorn config.wsgi --bind 0.0.0.0:$PORT --workers 3 --timeout 60
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
python manage.py collectstatic --noinput   # o admin precisa
python manage.py createsuperuser
```

---

## Passo 8 — Os dois cron jobs

Sem estes, o sistema degrada-se silenciosamente.

```
*/3 * * * * cd /app && python manage.py reconcile_payments
*/2 * * * * cd /app && python manage.py expire_tickets
```

O `reconcile_payments` apanha quem pagou e cujo webhook se perdeu. Em mobile
money isto acontece com frequência suficiente para importar: sem ele, essa
pessoa fica à porta com o dinheiro já fora da conta.

O `expire_tickets` liberta vagas de reservas não pagas. Sem ele, um evento
esgota com bilhetes que ninguém comprou.

Em Railway, use um serviço `cron` separado apontando ao mesmo repositório.

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
utilizador organizador) e deixe a API avisar quando cada bilhete é pago.

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
