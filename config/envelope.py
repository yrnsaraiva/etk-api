"""Envelope { status, message, data } — o formato que o cliente já espera."""

from rest_framework import status as http
from rest_framework.response import Response
from rest_framework.views import exception_handler


def ok(data=None, message="Success", status_code=http.HTTP_200_OK):
    return Response({"status": "success", "message": message, "data": data}, status=status_code)


def fail(message, status_code=http.HTTP_400_BAD_REQUEST, data=None):
    return Response({"status": "error", "message": message, "data": data}, status=status_code)


def _flatten(detail) -> str:
    if isinstance(detail, dict):
        parts = []
        for field, msgs in detail.items():
            msgs = msgs if isinstance(msgs, list) else [msgs]
            parts.append(f"{field}: {' '.join(str(m) for m in msgs)}")
        return " | ".join(parts)
    if isinstance(detail, list):
        return " ".join(str(m) for m in detail)
    return str(detail)


def envelope_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if response is None:
        return None
    data = response.data
    # DRF devolve dict ({"detail": ...} ou erros por campo) ou lista, conforme o caso
    detail = data.get("detail", data) if isinstance(data, dict) else data
    response.data = {"status": "error", "message": _flatten(detail), "data": None}
    return response
