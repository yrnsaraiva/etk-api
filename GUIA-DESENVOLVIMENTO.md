# Como construir a etk-api do zero

Doze etapas, pela ordem em que faz sentido construí-las. Cada etapa tem um
**ponto de verificação** — se não passar, pare aí. As etapas seguintes assumem
a anterior a funcionar.

A ordem não é arbitrária. O erro mais comum neste tipo de projeto é começar
pelas views: escreve-se o CRUD todo, fica bonito no Postman, e só no dia do
evento se descobre que a lógica de reserva tem uma corrida. Aqui o domínio vem
primeiro e é testado na shell antes de existir um único endpoint.

---

## Etapa 0 — Extrair o contrato (antes de escrever código)

Se está a replicar uma API que já existe, o contrato não é uma decisão sua: é
uma descoberta. Vá ao código que a consome e responda a seis perguntas.

```bash
grep -rn "requests\.\|http" --include=*.py .    # onde estão as chamadas
```

| Pergunta | No runwithbroto |
|---|---|
| Como autentica? | `Authorization: Bearer etk_live_…` |
| Que formato tem a resposta? | `{"status", "message", "data"}` |
| Como se chamam os campos? | camelCase — `imageUrl`, `priceId`, `fullName` |
| Que forma têm os IDs? | `EVNT` + epoch + 4 dígitos |
| Quem é o comprador? | um telefone `258XXXXXXXXX`, sem conta |
| Que verificações faz o cliente? | `message == "Ticket created successfully"` |

Essa última linha é o género de coisa que só se descobre a ler o cliente — e
que parte tudo se for ignorada. Escreva as respostas num ficheiro antes de
continuar; é a sua especificação.

**Verificação:** consegue descrever, sem olhar para o código, o que o cliente
envia e o que espera receber ao criar um bilhete.

---

## Etapa 1 — Esqueleto

```bash
mkdir etk-api && cd etk-api
python -m venv .venv && source .venv/bin/activate
pip install "django>=5.0,<5.2" djangorestframework djangorestframework-simplejwt django-filter requests
django-admin startproject config .
python manage.py startapp partners
python manage.py startapp catalog
python manage.py startapp ticketing
python manage.py startapp payments
```

Quatro apps porque há quatro responsabilidades distintas: quem vende
(`partners`), o que se vende (`catalog`), o que foi vendido (`ticketing`) e
como se recebe o dinheiro (`payments`). Uma app só chamada `core` funciona no
início e torna-se um ficheiro de 2000 linhas ao fim de seis meses.

A `payments` só ganha código na etapa 10, mas crie-a já — evita ter de mexer
em `INSTALLED_APPS` e nas migrações a meio do caminho.

Em `settings.py`, o essencial antes de qualquer modelo:

```python
INSTALLED_APPS += [
    "rest_framework", "django_filters",
    "partners", "catalog", "ticketing", "payments",
]
AUTH_USER_MODEL = "partners.User"     # tem de estar aqui ANTES da primeira migração
USE_TZ = True
```

O `AUTH_USER_MODEL` depois da primeira migração é uma das poucas coisas em
Django que obriga a apagar a base de dados e recomeçar. Defina-o já.

**Verificação:** `python manage.py check` dá 0 problemas.

---

## Etapa 2 — Identidade: quem chama a API

`partners/models.py` — o organizador e a chave que o site dele usa.

O ponto que interessa aqui não é o modelo, é **como se guarda a chave**. A
tentação é `key = CharField(...)` com o valor em claro. Não faça isso: se a
base de dados vazar, todas as chaves dos parceiros vão com ela.

```python
def _hash(raw): return hashlib.sha256(raw.encode()).hexdigest()

class ApiKey(models.Model):
    key_hash = models.CharField(max_length=64, unique=True, db_index=True)
    last_four = models.CharField(max_length=4)      # só para a UI
    revoked_at = models.DateTimeField(null=True, blank=True)

    @classmethod
    def issue(cls, owner, ...):
        raw = f"etk_{environment}_{secrets.token_urlsafe(18)[:24]}"
        key = cls.objects.create(key_hash=_hash(raw), last_four=raw[-4:], ...)
        return key, raw        # raw só existe aqui, nunca mais

    @classmethod
    def resolve(cls, raw):
        return cls.objects.filter(key_hash=_hash(raw), revoked_at__isnull=True).first()
```

Repare no `revoked_at` em vez de apagar a linha: quando revogar uma chave quer
manter o histórico de que existiu e quando foi usada pela última vez.

**Verificação, na shell:**

```python
key, raw = ApiKey.issue(user, label="teste")
assert ApiKey.resolve(raw) == key          # a chave certa resolve
assert ApiKey.resolve("etk_live_falsa") is None
key.revoke()
assert ApiKey.resolve(raw) is None          # revogada deixa de resolver
```

---

## Etapa 3 — Catálogo: o que se vende

`catalog/models.py` — `Event` e `Price`.

Duas decisões que a etapa 0 impôs. Primeira, os IDs são strings com prefixo, o
que significa `primary_key=True` num `CharField` e um gerador:

```python
def make_id(prefix):
    return f"{prefix}{int(time.time())}{random.randint(1000, 9999)}"
```

Segunda, o `Price` não é só um preço — é um **lote com stock**. É aqui que
mora a contagem que impede vender a mais:

```python
quantity_total = models.PositiveIntegerField()
quantity_reserved = models.PositiveIntegerField(default=0)   # pagos + pendentes

@property
def available(self):
    return max(self.quantity_total - self.quantity_reserved, 0)
```

Acrescente já a rede de segurança ao nível da base de dados:

```python
constraints = [
    models.CheckConstraint(
        condition=models.Q(quantity_reserved__lte=models.F("quantity_total")),
        name="price_no_oversell",
    )
]
```

O argumento chama-se `condition` desde a Django 5.1. Em versões anteriores era
`check`, e a 6.0 removeu-o de vez — se encontrar `check=` em exemplos antigos,
é essa a razão do `TypeError`.

Esta restrição nunca deve disparar. Se disparar, é sinal de que a lógica da
etapa 5 tem um buraco — e é infinitamente melhor rebentar com uma
`IntegrityError` do que vender um bilhete que não existe.

**Verificação:**

```python
p = Price.objects.create(event=ev, quantity_total=2)
assert p.available == 2
Price.objects.filter(pk=p.pk).update(quantity_reserved=3)   # tem de rebentar
```

---

## Etapa 4 — O bilhete

`ticketing/models.py`. Dois campos de estado, não um:

```python
status  = ...   # valid | cancelled | expired      → o bilhete em si
payment = ...   # pending | paid | failed | refunded → o dinheiro
entered = models.BooleanField(default=False)
expires_at = models.DateTimeField(null=True)   # prazo da reserva
```

Colapsar isto num único campo parece mais limpo e depois não consegue
representar "bilhete válido cujo pagamento falhou" nem "bilhete pago mas
cancelado pelo organizador". Mantenha separado.

O `expires_at` é o que permite libertar a vaga de quem não pagou.

**Verificação:** `python manage.py makemigrations && migrate` sem erros, e
consegue criar um bilhete na shell ligado a um `Price`.

---

## Etapa 5 — O coração: reservar sem vender a mais

Esta é a etapa que justifica a ordem toda. Escreva-a em
`ticketing/services.py`, **não numa view**.

O código ingénuo:

```python
if price.available >= 1:          # ← duas chamadas leem "resta 1"
    price.quantity_reserved += 1  # ← as duas passam
    price.save()
```

Dois compradores a carregar no botão ao mesmo tempo passam ambos aqui. O que
funciona:

```python
@transaction.atomic
def create_ticket(*, price_id, event_id, phone, issued_to, ...):
    price = Price.objects.select_for_update().select_related("event").get(pk=price_id)

    if not price.is_on_sale():
        raise TicketError("Bilhetes esgotados." if price.available == 0 else "...")

    Price.objects.filter(pk=price.pk).update(quantity_reserved=F("quantity_reserved") + 1)

    return Ticket.objects.create(
        price=price, phone=phone, issued_to=issued_to,
        expires_at=timezone.now() + timedelta(minutes=15),
    )
```

Três coisas a fazer o trabalho, e vale a pena perceber cada uma:

`select_for_update()` tranca a linha do `Price` até ao fim da transação. A
segunda chamada fica em espera na base de dados e só lê depois de a primeira
ter escrito — vê `available == 0` e é recusada.

`F("quantity_reserved") + 1` faz a soma dentro da base de dados. Se escrevesse
`price.quantity_reserved + 1` em Python, estaria a somar sobre um valor lido
antes do lock de outra pessoa.

`@transaction.atomic` garante que, se a criação do bilhete falhar, a reserva
é desfeita.

Escreva também o inverso, porque vai precisar dele em três sítios (expiração,
cancelamento, pagamento falhado):

```python
@transaction.atomic
def release(ticket, payment_status):
    Price.objects.filter(pk=ticket.price_id).update(
        quantity_reserved=F("quantity_reserved") - 1
    )
    ...
```

**Verificação — e faça-a a sério.** Precisa de PostgreSQL: no SQLite o
`select_for_update()` não tranca nada e o teste passa por acidente.

```bash
docker run -d -e POSTGRES_PASSWORD=dev -e POSTGRES_DB=etk -p 5432:5432 postgres:16
export DATABASE_URL=postgresql://postgres:dev@localhost:5432/etk
python manage.py migrate
python manage.py shell < concurrency_test.py
```

O `concurrency_test.py` lança 10 threads a comprar 3 bilhetes ao mesmo
instante (uma `threading.Barrier` garante que arrancam juntas). Com Postgres o
resultado tem de ser exatamente 3 sucessos e 7 recusas limpas, zero erros. Se
der 4 sucessos, tem uma corrida — não avance.

Corra-o primeiro em SQLite, só para ver o que acontece:

```
sucessos         : 1
recusas          : 0
erros inesperados: 9 ["OperationalError('database is locked')", ...]
```

Nove das dez threads rebentam em vez de esperarem pela sua vez. Não é a corrida
que queríamos testar — é o SQLite a admitir que não serve. É por isto que a
etapa de PostgreSQL não é um detalhe de produção.

---

## Etapa 6 — Falar o dialeto do cliente

Só agora se pensa em JSON. Duas peças.

O envelope, em `config/envelope.py`:

```python
def ok(data=None, message="Success", status_code=200):
    return Response({"status": "success", "message": message, "data": data}, status=status_code)
```

E um `exception_handler` que converte os erros do DRF para o mesmo formato —
senão os erros saem em `{"detail": ...}` e o cliente não os sabe ler.

A serialização camelCase, como método nos modelos:

```python
def to_api(self):
    return {
        "id": self.id,
        "imageUrl": self.image_url,
        "location": {"province": self.province, "details": self.location_details},
        "prices": [p.to_api() for p in self.prices.all()],
        "totalTicketsPurchased": self.total_tickets_purchased,
    }
```

Serializers do DRF também davam, mas para uma forma fixa e imposta por
terceiros um método é mais direto de ler e de manter alinhado com o contrato.
Guarde os serializers para **validar entrada** — é aí que valem a pena.

**Verificação:** `json.dumps(event.to_api())` produz exatamente a estrutura
que escreveu na etapa 0.

---

## Etapa 7 — Autenticação por chave

`partners/authentication.py`:

```python
class ApiKeyAuthentication(authentication.BaseAuthentication):
    def authenticate(self, request):
        header = authentication.get_authorization_header(request).split()
        if not header or header[0].decode().lower() != "bearer":
            return None
        raw = header[1].decode()
        if not raw.startswith("etk_"):
            return None          # deixa passar para o JWT
        api_key = ApiKey.resolve(raw)
        if api_key is None:
            raise exceptions.AuthenticationFailed("Chave inválida ou revogada.")
        return (api_key.owner, api_key)
```

O `return None` quando não começa por `etk_` é o truque que deixa as duas
autenticações conviverem: chaves de parceiro e JWT de organizador, no mesmo
cabeçalho `Bearer`, sem se atropelarem.

**Verificação:** um `curl` com chave válida dá 200, com chave inventada dá 401,
com chave revogada dá 401.

---

## Etapa 8 — Os endpoints externos

Agora sim, as views — e são finas, porque a lógica já está toda feita:

```python
class ExternalTicketCreateView(APIView):
    authentication_classes = [ApiKeyAuthentication]

    def post(self, request):
        s = TicketCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        ticket = create_ticket(**...)                    # etapa 5
        return ok(ticket.to_api(), "Ticket created successfully", 201)
```

Se uma view sua tiver mais de umas 15 linhas, provavelmente tem regra de
negócio lá dentro que devia estar em `services.py`.

As rotas seguem o contrato, incluindo a ausência de barra final:

```python
path("back/borrow/external/events", ExternalEventListView.as_view()),
path("back/borrow/external/events/<str:event_id>", ExternalEventDetailView.as_view()),
path("back/borrow/external/tickets", ExternalTicketCreateView.as_view()),
```

**Verificação — a mais importante do projeto.** Copie as funções do cliente
real (`_etk_request`, `_get_events_from_api`, `_create_ticket_in_api`,
`_build_event_context`) para um script, mude só o `ETK_BASE`, e corra-o. Se
passar sem tocar no cliente, o contrato está certo. É o `client_compat_test.py`.

---

## Etapa 9 — Acrescentar o que falta ao contrato original

Aqui deixa de replicar e começa a melhorar. Três buracos que a API original
tem e que o cliente sofre:

**`GET /tickets/{id}`.** Sem isto, o parceiro grava `payment: pending` na
criação e nunca mais sabe que o bilhete foi pago. É a causa do bug do scanner
no runwithbroto.

**Webhook para o parceiro.** O inverso do anterior: em vez de ele perguntar,
você avisa. Campo `webhook_url` no organizador, corpo assinado com HMAC.

**Check-in no servidor, com QR assinado.** O QR do runwithbroto é
`RWB|<id>` — quem perceber o formato entra sem bilhete. Assine-o:

```python
@property
def qr_value(self):
    sig = hmac.new(settings.SECRET_KEY.encode(), self.id.encode(), hashlib.sha256)
    return f"{self.id}|{sig.hexdigest()[:16]}"
```

O check-in também precisa de `select_for_update()`, pela mesma razão da etapa
5: dois leitores a scanear o mesmo bilhete em simultâneo.

**Verificação:** o mesmo QR autoriza à primeira e é recusado à segunda; um QR
com assinatura adulterada dá `invalid_qr`.

---

## Etapa 10 — A app payments: a porta, antes do gateway

Contra-intuitivo mas poupa muito tempo: defina a **interface** e escreva um
gateway falso antes de tocar no gateway real. Assim testa o fluxo inteiro sem
depender de credenciais de ninguém — e quando o DebitoPay chegar, é só mais uma
implementação da mesma interface.

### 10.1 — Estrutura da app

```bash
mkdir -p payments/providers payments/management/commands
touch payments/providers/__init__.py
touch payments/management/__init__.py payments/management/commands/__init__.py
```

```
payments/
├── models.py                 ProviderEvent (idempotência)
├── services.py               orquestração: iniciar, aplicar webhook, reconciliar
├── views.py                  o endpoint do webhook
├── providers/
│   ├── base.py               a interface + dataclasses + exceções
│   ├── fake.py               gateway falso, para testes
│   ├── debitopay.py          o real (etapa 11)
│   └── registry.py           escolhe o provider pelo settings
└── management/commands/
    ├── reconcile_payments.py
    └── expire_tickets.py
```

A subpasta `providers/` existe para que a resposta a "trocar de gateway" seja
"acrescentar um ficheiro", não "procurar `requests.post` por todo o projeto".

### 10.2 — A interface

`payments/providers/base.py`. Três métodos, dois dataclasses, duas exceções:

```python
PENDING, SUCCEEDED, FAILED = "pending", "succeeded", "failed"

class PaymentError(Exception): ...
class InvalidSignature(PaymentError): ...

@dataclass(frozen=True)
class Charge:
    reference: str; status: str; amount: Decimal; currency: str
    checkout_url: str = ""; instructions: str = ""; raw: dict = field(default_factory=dict)

@dataclass(frozen=True)
class WebhookEvent:
    event_id: str; type: str; charge_reference: str
    status: str; amount: Decimal | None; currency: str; raw: dict

class PaymentProvider(ABC):
    @abstractmethod
    def create_charge(self, *, amount, currency, reference, phone,
                      method, description, callback_url) -> Charge: ...
    @abstractmethod
    def fetch_charge(self, reference: str) -> Charge: ...
    @abstractmethod
    def parse_webhook(self, body: bytes, headers) -> WebhookEvent: ...
```

Três estados apenas, e cada gateway traduz o seu vocabulário para estes. Se
deixar `authorized`, `settled`, `captured` e companhia entrarem no domínio,
a lógica de negócio passa a conhecer as manias de um fornecedor específico.

`InvalidSignature` é uma exceção separada de propósito: um webhook mal assinado
não é um erro de comunicação, é uma tentativa de fraude, e trata-se de outra
maneira.

### 10.3 — Configuração e registo

Em `settings.py`:

```python
PAYMENT_PROVIDER = os.getenv("PAYMENT_PROVIDER", "debitopay")
PAYMENT_PROVIDERS = {
    "debitopay": "payments.providers.debitopay.DebitoPayProvider",
    "fake": "payments.providers.fake.FakeProvider",
}
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8000")
```

`payments/providers/registry.py` resolve a string para a classe:

```python
def get_provider(name=None):
    name = name or settings.PAYMENT_PROVIDER
    if name not in _instances:
        _instances[name] = import_string(settings.PAYMENT_PROVIDERS[name])()
    return _instances[name]
```

É isto que permite `PAYMENT_PROVIDER=fake` numa variável de ambiente trocar
todo o comportamento de pagamento sem editar uma linha.

### 10.4 — O gateway falso

`payments/providers/fake.py` guarda cobranças num dicionário e, além dos três
métodos, expõe dois auxiliares que só fazem sentido em testes:

```python
def settle(self, reference, status=SUCCEEDED):
    """Simula o cliente a confirmar no telemóvel."""

def build_webhook(self, reference, status=SUCCEEDED, amount=None, event_id=None):
    """Devolve (body, headers) com assinatura HMAC válida."""
```

O `build_webhook` a aceitar `amount` e `event_id` não é acidental: é o que lhe
permite testar o webhook duplicado (mesmo `event_id`) e o valor adulterado
(`amount` diferente do preço) na etapa 11.

### 10.5 — Campos novos no Ticket

O bilhete precisa de saber a que cobrança corresponde. Em `ticketing/models.py`:

```python
provider = models.CharField(max_length=40, blank=True)
provider_charge_id = models.CharField(max_length=128, blank=True, db_index=True)
checkout_url = models.URLField(blank=True)
```

O `db_index` no `provider_charge_id` é obrigatório na prática: é por aí que o
webhook encontra o bilhete, e é a consulta mais frequente do sistema em dia de
venda.

```bash
python manage.py makemigrations payments ticketing && python manage.py migrate
```

**Verificação:**

```python
from payments.providers.registry import get_provider
gw = get_provider("fake")
c = gw.create_charge(amount=Decimal("300"), currency="MZN", reference="TCKT1",
                     phone="258840000000", method="mobile_money",
                     description="teste", callback_url="http://x")
assert c.status == "pending"
body, hdr = gw.build_webhook(c.reference)
assert gw.parse_webhook(body, hdr).status == "succeeded"
try:
    gw.parse_webhook(body, {"X-Debito-Signature": "falsa"}); assert False
except InvalidSignature:
    pass
```

---

## Etapa 11 — Orquestração e o gateway real

### 11.1 — A tabela da idempotência

`payments/models.py`:

```python
class ProviderEvent(models.Model):
    provider = models.CharField(max_length=40)
    event_id = models.CharField(max_length=128)
    charge_reference = models.CharField(max_length=128, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    outcome = models.CharField(max_length=200, blank=True)
    payload = models.JSONField(default=dict, blank=True)

    constraints = [models.UniqueConstraint(fields=["provider", "event_id"],
                                           name="uniq_provider_event")]
```

Toda a defesa está naquela `UniqueConstraint`. Os gateways reenviam quando não
recebem 200 a tempo; sem esta tabela, o reenvio confirma o bilhete outra vez.

### 11.2 — Os três serviços

`payments/services.py`:

```python
def start_payment(ticket, *, callback_url, provider_name=None) -> Charge:
    """Cria a cobrança e guarda provider_charge_id no bilhete."""

def handle_webhook(body: bytes, headers, provider_name=None) -> tuple[bool, str]:
    """Valida assinatura, grava o evento, aplica. Devolve (processado, mensagem)."""

def reconcile_pending(limit=200) -> dict:
    """Sonda o gateway sobre bilhetes pendentes."""
```

O `handle_webhook` grava **antes** de aplicar, e é a `IntegrityError` que
deteta o duplicado:

```python
try:
    with transaction.atomic():
        record = ProviderEvent.objects.create(provider=..., event_id=event.event_id, ...)
except IntegrityError:
    return False, "Evento já recebido (ignorado)."
outcome = _apply(event)
```

Fazer a verificação com um `if ProviderEvent.objects.filter(...).exists()`
teria a mesma corrida da etapa 5: dois reenvios simultâneos passam ambos. A
restrição da base de dados não tem esse problema.

E dentro do `_apply`, a verificação que nenhum tutorial de gateway inclui:

```python
if event.amount is not None and Decimal(event.amount) != ticket.price.amount:
    logger.error("valor divergente no bilhete %s: cobrado %s, esperado %s", ...)
    return "Valor divergente — retido para revisão manual."
```

Note o **retido**, não rejeitado nem confirmado. Se os valores não batem, algo
correu mal e você não sabe o quê — a decisão é de uma pessoa, não do código.

### 11.3 — O endpoint do webhook

`payments/views.py`, e uma regra de ouro:

```python
@api_view(["POST"])
@authentication_classes([])          # o gateway não tem conta nem JWT
@permission_classes([AllowAny])      # a assinatura é que autentica
def debitopay_webhook(request):
    try:
        processed, message = handle_webhook(request.body, request.headers)
    except InvalidSignature:
        return fail("Assinatura inválida.", 401)
    return ok({"processed": processed}, message)
```

Responda **200 a tudo o que seja autêntico**, mesmo a eventos que ignora. Um
4xx faz o gateway reenviar em ciclo sem que isso resolva nada — só o 401 da
assinatura é que faz sentido, e esse é para o atacante, não para o gateway.

Use `request.body` (bytes crus), nunca `request.data`. O DRF já parseou e
re-serializou o JSON, e a assinatura foi calculada sobre os bytes originais.

Nas rotas:

```python
path("back/payments/webhooks/debitopay", debitopay_webhook),
```

### 11.4 — Ligar à criação do bilhete

Em `ticketing/views.py`, depois do `create_ticket`:

```python
try:
    charge = start_payment(
        ticket,
        callback_url=f"{settings.PUBLIC_BASE_URL}/back/payments/webhooks/debitopay",
    )
except PaymentError as exc:
    return fail("Não foi possível iniciar o pagamento. Tente novamente.",
                502, data={"ticketId": ticket.id})
```

Repare que a vaga **fica reservada** quando o gateway falha. O cliente pode
tentar de novo sem perder o lugar, e o `expire_tickets` limpa se ele desistir.
A alternativa — libertar já — favorece o organizador em eventos disputados.
É uma decisão de negócio, tome-a conscientemente.

### 11.5 — Os dois comandos

`payments/management/commands/reconcile_payments.py` e `expire_tickets.py`,
cada um com quatro linhas a chamar a função respetiva. No cron:

```
*/3 * * * * python manage.py reconcile_payments
*/2 * * * * python manage.py expire_tickets
```

Sem o primeiro, quem paga e cujo webhook se perde fica à porta com o dinheiro
já fora. Sem o segundo, o evento esgota com bilhetes que ninguém comprou.

### 11.6 — Só agora o DebitoPay

`payments/providers/debitopay.py`, implementando a mesma interface da etapa
10.2. Como a documentação pública não publica o contrato, marque cada suposição
com um comentário `CONTRATO-N` e confirme-as no dashboard.

**Verificação:** `PAYMENT_PROVIDER=fake python manage.py shell < payments_test.py`
cobre os quatro casos — webhook duplicado ignorado, assinatura falsa recusada,
valor adulterado retido, webhook perdido recuperado pela reconciliação.

---

## Etapa 12 — A área do organizador

Por fim o CRUD com JWT: criar eventos, criar lotes, emitir chaves, ver o
dashboard. Deixei para o fim de propósito — é a parte mais fácil e a menos
arriscada. ViewSets normais do DRF, filtrados por `organizer=request.user`.

Um detalhe: no `POST /api-keys/`, o valor em claro da chave vai **só nessa
resposta**. Se a puder mostrar outra vez, é porque a guardou em claro — e
voltou à etapa 2.

---

## O que verificar quando algo parte

| Sintoma | Onde olhar |
|---|---|
| Vendeu mais bilhetes do que existem | Etapa 5 — está a correr em SQLite? |
| Cliente recebe "erro interno" em vez da mensagem | Etapa 6 — o `exception_handler` |
| Bilhete pago fica `pending` para sempre | Etapa 11 — webhook não chega, falta reconciliação |
| Webhook dá sempre 401 | Etapa 11 — assina o corpo cru ou o JSON re-serializado? |
| Vagas presas em reservas mortas | Falta o cron do `expire_tickets` |
| Bilhete entra duas vezes | Etapa 9 — falta `select_for_update` no check-in |
