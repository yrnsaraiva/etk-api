"""Diagnóstico da ligação à iMali.Way — correr NO SERVIDOR (é o IP dele que conta).

    python manage.py check_imaliway                       # IP, ligação e credenciais (só leitura)
    python manage.py check_imaliway --status REF          # estado de uma cobrança
    python manage.py check_imaliway --pay 841234567       # push de teste na sandbox (mín. 10 MT)

Responde, por ordem, às três perguntas que costumam falhar:

1. Qual é o IP de saída deste servidor? (é o que a Paytek tem de pôr na whitelist)
2. A ligação à porta da iMali.Way abre? (timeout/reset = IP não autorizado)
3. As credenciais são aceites? (a consulta de estado é só de leitura)

Nunca imprime segredos. Por omissão usa as credenciais de sandbox.
"""

import socket
import time
import uuid
from decimal import Decimal
from urllib.parse import urlparse

import requests
from django.core.management.base import BaseCommand, CommandError

from payments import imaliway
from payments.exceptions import PaymentDeclined, PaymentError, ProviderUnavailable

CAMPOS = [
    ("API_KEY", "API_KEY"),
    ("CLIENT_ID", "CLIENT_ID"),
    ("PUBLIC_KEY", "PUBLIC_KEY"),
    ("STORE_ACCOUNT_NUMBER", "STORE_ACCOUNT_NUMBER"),
    ("WEBHOOK_SECRET", "WEBHOOK_SECRET"),
]


class Command(BaseCommand):
    help = "Diagnostica a ligação à iMali.Way: IP de saída, porta, credenciais (e, opcionalmente, um push de teste)."

    def add_arguments(self, parser):
        parser.add_argument("--live", action="store_true",
                            help="Usa as credenciais live em vez das de sandbox.")
        parser.add_argument("--status", metavar="REF",
                            help="Consulta o estado de um partner_transaction_id.")
        parser.add_argument("--pay", metavar="TELEFONE",
                            help="Cria um push de teste para este número (9 dígitos).")
        parser.add_argument("--amount", type=Decimal, default=Decimal("10"),
                            help="Valor do push de teste (mínimo 10 MT). Por omissão 10.")
        parser.add_argument("--method", default="mpesa", help="mpesa | emola | mkesh | imali")
        parser.add_argument("--confirmar-dinheiro-real", action="store_true",
                            help="Obrigatório para usar --pay com --live (cobra dinheiro a sério).")

    # ── helpers de saída ──────────────────────────────────────────────────────
    def ok(self, msg):
        self.stdout.write(self.style.SUCCESS(f"  ✓ {msg}"))

    def bad(self, msg):
        self.stdout.write(self.style.ERROR(f"  ✗ {msg}"))

    def warn(self, msg):
        self.stdout.write(self.style.WARNING(f"  ! {msg}"))

    def title(self, msg):
        self.stdout.write(f"\n{msg}")

    # ── passos ────────────────────────────────────────────────────────────────
    def handle(self, *args, **opts):
        sandbox = not opts["live"]
        if opts["pay"] and opts["live"] and not opts["confirmar_dinheiro_real"]:
            raise CommandError("--pay com --live cobra dinheiro real. Se é mesmo isso, junte --confirmar-dinheiro-real.")

        cfg = imaliway._cfg(sandbox)
        prefix = "IMALIWAY_SANDBOX_" if sandbox else "IMALIWAY_"
        self.stdout.write(f"iMali.Way — ambiente: {'SANDBOX' if sandbox else 'LIVE'}")
        self.stdout.write(f"URL base: {cfg['BASE_URL']}")

        self.title("1. IP de saída deste servidor")
        self.passo_ip()

        self.title("2. Configuração (só diz se está preenchida)")
        faltam = self.passo_config(cfg, prefix)

        self.title("3. Ligação à porta da iMali.Way")
        liga = self.passo_ligacao(cfg["BASE_URL"])

        if faltam or not liga:
            self.title("Resultado")
            self.bad("Parou antes de testar as credenciais." + (
                " Preencha as variáveis em falta." if faltam else
                " Sem ligação à porta, o pedido nunca chega à Paytek."))
            return

        self.title("4. Credenciais (consulta de estado, só leitura)")
        self.passo_credenciais(sandbox)

        if opts["status"]:
            self.title(f"Estado de {opts['status']}")
            self.passo_estado(opts["status"], sandbox)
        if opts["pay"]:
            self.title("Push de teste")
            self.passo_pagar(opts, sandbox)

    def passo_ip(self):
        try:
            ip = requests.get("https://api.ipify.org", timeout=8).text.strip()
            self.ok(f"IP de saída: {ip}")
            self.stdout.write("    → É este IP que a Paytek tem de pôr na whitelist. "
                              "Em hosts com IP dinâmico (ex.: Railway sem IP fixo) pode mudar a cada deploy.")
        except requests.RequestException as exc:
            self.warn(f"Não foi possível descobrir o IP de saída ({type(exc).__name__}).")

    def passo_config(self, cfg, prefix):
        faltam = []
        for chave, nome in CAMPOS:
            if cfg.get(chave):
                self.ok(f"{prefix}{nome} preenchida")
            else:
                self.bad(f"{prefix}{nome} em falta")
                faltam.append(nome)
        if cfg.get("PUBLIC_KEY"):
            try:
                imaliway._load_public_key(cfg["PUBLIC_KEY"])
                self.ok("publicKey lida (PEM válido)")
            except PaymentError as exc:
                self.bad(str(exc))
                faltam.append("PUBLIC_KEY")
        return faltam

    def passo_ligacao(self, base_url):
        parsed = urlparse(base_url)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        inicio = time.monotonic()
        try:
            socket.create_connection((host, port), timeout=10).close()
        except socket.timeout:
            self.bad(f"{host}:{port} — timeout ao ligar (10 s). Os pacotes são descartados: "
                     "é o comportamento típico de uma firewall que não conhece este IP.")
            return False
        except ConnectionRefusedError:
            self.bad(f"{host}:{port} — ligação recusada. Porta fechada ou bloqueada.")
            return False
        except OSError as exc:
            self.bad(f"{host}:{port} — {type(exc).__name__}: {exc}")
            return False
        self.ok(f"{host}:{port} abre ({time.monotonic() - inicio:.1f} s)")
        return True

    def passo_credenciais(self, sandbox):
        ref = "DIAGNOSTICO"
        try:
            charge = imaliway.fetch_charge(ref, sandbox=sandbox)
        except PaymentDeclined as exc:
            # O gateway respondeu e avaliou o pedido (cobrança inexistente): credenciais aceites.
            self.ok(f"Credenciais aceites — a iMali.Way respondeu (código {exc.code}) a uma "
                    "cobrança que não existe, como esperado.")
        except ProviderUnavailable as exc:
            self.bad(f"Sem resposta utilizável: {exc}")
        except PaymentError as exc:
            self.bad(str(exc))
            self.stdout.write("    → 'Invalid token'/500 com credenciais certas: confirme que a api_key e a "
                              "publicKey são as do ambiente certo. Se estão, o formato da privateKey "
                              "(assumimos base64) pode ser outro.")
        else:
            self.ok(f"Credenciais aceites (estado devolvido: {charge.status}).")

    def passo_estado(self, ref, sandbox):
        try:
            charge = imaliway.fetch_charge(ref, sandbox=sandbox)
        except PaymentError as exc:
            self.bad(str(exc))
            return
        self.ok(f"{ref}: {charge.status}  {charge.raw}")

    def passo_pagar(self, opts, sandbox):
        ref = f"DIAG{uuid.uuid4().hex[:8].upper()}"       # id de bilhete fictício, só para este teste
        try:
            charge = imaliway.create_charge(
                amount=opts["amount"], currency="MZN", reference=ref, phone=opts["pay"],
                method=opts["method"], sandbox=sandbox)
        except PaymentError as exc:
            self.bad(str(exc))
            return
        self.ok(f"Push criado — estado {charge.status}. Aprove no telemóvel.")
        self.stdout.write(f"    partner_transaction_id: {charge.reference}")
        self.stdout.write(f"    Para ver o resultado: python manage.py check_imaliway "
                          f"{'' if sandbox else '--live '}--status {charge.reference}")
