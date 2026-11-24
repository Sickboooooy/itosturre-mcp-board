"""IT-005 · Identidad del servidor.

- Riesgo 1: el token de pruebas firma como cualquiera, así que solo vale con VERCEL_ENV
  exactamente igual a un entorno de prueba de la lista permitida. Ausente o desconocido: cerrado.
- Riesgo 7: dos variables con el mismo token dejan la identidad ambigua: el servidor no arranca
  (503) y el log dice qué variables chocan, sin mostrar el valor.

Slack y Redis están simulados (ver conftest.py); nada sale a la red.
"""

from __future__ import annotations

import logging

import pytest
from conftest import TOKENS, llamar

import coordinacion

pytestmark = pytest.mark.anyio

CLAUDE = TOKENS["MCP_TOKEN"]
CODEX = TOKENS["MCP_TOKEN_CODEX"]
PRUEBAS = TOKENS["MCP_TOKEN_PRUEBAS"]

TRASPASO = {
    "agente": "Codex",
    "encargo_id": "IG-01",
    "version": "v2",
    "entrega_a": "Muse",
    "enlace": "https://docs.google.com/document/d/ficticio-v2",
    "nota": "Entrega de prueba.",
    "clave_idempotencia": "pruebas-ig01-v2-0001",
    "confirmar": True,
}


async def _tools_list(cliente_http, token: str):
    return await cliente_http.post(
        "/mcp",
        headers={"authorization": f"Bearer {token}", "accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )


# --- riesgo 1: token de pruebas solo en entornos de prueba explícitos ---
@pytest.mark.parametrize("valor", [None, "", "Production", "production ", " preview", "PREVIEW", "prod", "staging"])
async def test_token_de_pruebas_cerrado_si_el_entorno_no_es_de_prueba(cliente_http, board, monkeypatch, valor):
    almacen, slack = board
    if valor is None:
        monkeypatch.delenv("VERCEL_ENV", raising=False)
    else:
        monkeypatch.setenv("VERCEL_ENV", valor)
    estado, _, _ = await llamar(cliente_http, PRUEBAS, "registrar_traspaso", TRASPASO)
    assert estado == 401
    assert slack.peticiones == []
    assert almacen.encargo["version"] == "v1"


@pytest.mark.parametrize("valor", ["preview", "development"])
async def test_token_de_pruebas_vale_en_entornos_de_prueba(cliente_http, board, monkeypatch, valor):
    monkeypatch.setenv("VERCEL_ENV", valor)
    estado, es_error, datos = await llamar(cliente_http, PRUEBAS, "obtener_encargo_vigente")
    assert estado == 200 and not es_error, datos


async def test_la_firma_es_una_segunda_barrera_para_el_token_de_pruebas(board, monkeypatch):
    """Si index.py dejara pasar el token de pruebas por error, la firma lo rechaza igual."""
    _, slack = board
    monkeypatch.delenv("VERCEL_ENV", raising=False)
    ficha = coordinacion.cliente_actual.set(coordinacion.CLIENTE_PRUEBAS)
    try:
        with pytest.raises(coordinacion.ErrorBoard) as error:
            await coordinacion.registrar_traspaso(**TRASPASO)
    finally:
        coordinacion.cliente_actual.reset(ficha)
    assert error.value.codigo == "token_de_pruebas_no_permitido"
    assert slack.peticiones == []


async def test_solo_token_de_pruebas_y_sin_entorno_es_servidor_sin_configurar(cliente_http, entorno, monkeypatch):
    for variable in TOKENS:
        if variable != "MCP_TOKEN_PRUEBAS":
            monkeypatch.delenv(variable, raising=False)
    monkeypatch.delenv("VERCEL_ENV", raising=False)
    respuesta = await _tools_list(cliente_http, PRUEBAS)
    assert respuesta.status_code == 503
    assert respuesta.json()["error"] == "servidor_sin_configurar"


# --- riesgo 7: tokens repetidos ---
async def test_tokens_repetidos_fallan_cerrado(cliente_http, entorno, monkeypatch, caplog):
    monkeypatch.setenv("MCP_TOKEN_MUSE", CODEX)
    caplog.set_level(logging.DEBUG)
    for token in (CODEX, CLAUDE):
        respuesta = await _tools_list(cliente_http, token)
        assert respuesta.status_code == 503
        assert respuesta.json()["error"] == "configuracion_invalida"
    assert "MCP_TOKEN_CODEX" in caplog.text and "MCP_TOKEN_MUSE" in caplog.text
    assert CODEX not in caplog.text


async def test_tokens_repetidos_aunque_difieran_en_espacios(cliente_http, entorno, monkeypatch):
    monkeypatch.setenv("MCP_TOKEN_MUSE", f"  {CODEX}\n")
    respuesta = await _tools_list(cliente_http, CLAUDE)
    assert respuesta.status_code == 503
    assert respuesta.json()["error"] == "configuracion_invalida"


async def test_token_de_pruebas_repetido_falla_tambien_en_produccion(cliente_http, entorno, monkeypatch):
    """En Producción el token de pruebas se ignora, pero repetir un token real sigue siendo un error de configuración."""
    monkeypatch.setenv("VERCEL_ENV", "production")
    monkeypatch.setenv("MCP_TOKEN_PRUEBAS", CLAUDE)
    respuesta = await _tools_list(cliente_http, CLAUDE)
    assert respuesta.status_code == 503
    assert respuesta.json()["error"] == "configuracion_invalida"


async def test_tokens_distintos_siguen_funcionando(cliente_http, entorno):
    respuesta = await _tools_list(cliente_http, CODEX)
    assert respuesta.status_code == 200
