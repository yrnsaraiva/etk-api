"""Fase 0.3: DJANGO_SECRET_KEY é obrigatória — sem valor por omissão. Como o
settings.py só é importado uma vez por processo, a única forma fiável de
provar que falta a variável faz o arranque falhar é num subprocesso novo."""

import os
import subprocess
import sys
from pathlib import Path

from django.test import SimpleTestCase

BASE_DIR = Path(__file__).resolve().parent.parent


class SecretKeyObrigatoriaTests(SimpleTestCase):
    def test_arrancar_sem_django_secret_key_falha(self):
        env = {k: v for k, v in os.environ.items() if k != "DJANGO_SECRET_KEY"}
        result = subprocess.run(
            [sys.executable, "manage.py", "check"],
            cwd=BASE_DIR, env=env, capture_output=True, text=True, timeout=30,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DJANGO_SECRET_KEY", result.stderr)
