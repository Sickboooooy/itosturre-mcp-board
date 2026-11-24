"""IT-002 · Llave de solo lectura para Hermes (MCP_TOKEN_HERMES).

Mismo patrón que MCP_TOKEN_OPENAI: el cliente `hermes` lee, pero toda herramienta que escribe
responde `solo_lectura` sin tocar el almacén ni Slack. Hermes no firma (no es agente ni
destinatario) y su variable entra en la comprobación de tokens repetidos (503).

Slack y Redis están simulados (ver conftest.py); nada sale a la red.
"""

from __future__ import annotations

import json
import logging
import typing

import pytest
from conftest import TOKENS, llamar

import coordinacion

pytestmark = pytest.mark.anyio

HERMES = TOKENS["MCP_TOKEN_HERMES"]
CODEX = TOKENS["MCP_TOKEN_CODEX"]
OPENAI = TOKENS["MCP_TOKEN_OPENAI"]

AGENTES = typing.get_args(coordinacion.AgenteNombre)
HERRAMIENTAS_DE_LECTURA = ("obtener_tarea_instagram", "obtener_encargo_vigente", "listar_traspasos")
HERRAMIENTAS_DE_ESCRITURA = ("registrar_traspaso", "registrar_acuse", "renovar_arriendo", "recuperar_encargo")

TRASPASO_CODEX = {
    "agente": "Codex",
    "encargo_id": "IG-01",
    "version": "v2",
    "entrega_a": "Claude",
    "enlace": "https://docs.google.com/document/d/ficticio-v2",
    "nota": "Entrega de prueba.",
    "clave_idempotencia": "codex-ig01-v2-0001",
    "confirmar": True,
}


async def _tools_list(cliente_http, token: str):
    return await cliente_http.post(
        "/mcp",
        headers={"authorization": f"Bearer {token}", "accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )


def _foto(almacen) -> str:
    """Todo lo que guarda el almacén en memoria, en un texto comparable."""
    return json.dumps(
        {
            "encargo": almacen.encargo,
            "traspasos": almacen.traspasos,
            "orden": almacen.orden,
            "idem": almacen.idem,
            "ttl": {repr(clave): segundos for clave, segundos in almacen.ttl.items()},
        },
        sort_keys=True,
        default=str,
    )


def _argumentos(herramienta: str, agente: str, traspaso_id: str) -> dict:
    """Argumentos válidos para cada herramienta que escribe: lo único que falla es el token."""
    if herramienta == "registrar_traspaso":
        return {
            "agente": agente,
            "encargo_id": "IG-01",
            "version": "v3",
            "entrega_a": "Codex" if agente != "Codex" else "Muse",
            "enlace": "https://docs.google.com/document/d/ficticio-v3",
            "nota": "Hermes intenta escribir.",
            "clave_idempotencia": "hermes-traspaso-0001",
            "confirmar": True,
        }
    if herramienta == "registrar_acuse":
        return {"agente": agente, "traspaso_id": traspaso_id, "clave_idempotencia": "hermes-acuse-0001", "confirmar": True}
    if herramienta == "renovar_arriendo":
        return {"agente": agente, "encargo_id": "IG-01", "clave_idempotencia": "hermes-renueva-0001", "confirmar": True}
    if herramienta == "recuperar_encargo":
        return {"agente": agente, "encargo_id": "IG-01", "clave_idempotencia": "hermes-recupera-0001", "confirmar": True, "nota": "Hermes intenta tomarlo."}
    raise AssertionError(f"herramienta de escritura sin argumentos de prueba: {herramienta}")


# --- lectura ---
async def test_hermes_autentica_y_lee(cliente_http, board):
    respuesta = await _tools_list(cliente_http, HERMES)
    assert respuesta.status_code == 200
    estado, es_error, encargo = await llamar(cliente_http, HERMES, "obtener_encargo_vigente")
    assert estado == 200 and not es_error and encargo["id"] == "IG-01", encargo
    estado, es_error, lista = await llamar(cliente_http, HERMES, "listar_traspasos")
    assert estado == 200 and not es_error and lista == {"traspasos": []}, lista
    estado, es_error, tarea = await llamar(cliente_http, HERMES, "obtener_tarea_instagram")
    assert estado == 200 and not es_error and tarea["id"] == "TAREA-PRUEBA-001", tarea


async def test_hermes_las_herramientas_probadas_son_todas(cliente_http, entorno):
    """Si aparece otra herramienta que escribe, esta prueba obliga a cubrirla abajo."""
    respuesta = await _tools_list(cliente_http, HERMES)
    assert respuesta.status_code == 200
    herramientas = respuesta.json()["result"]["tools"]
    escritura = {h["name"] for h in herramientas if not (h.get("annotations") or {}).get("readOnlyHint")}
    lectura = {h["name"] for h in herramientas if (h.get("annotations") or {}).get("readOnlyHint")}
    assert escritura == set(HERRAMIENTAS_DE_ESCRITURA)
    assert lectura == set(HERRAMIENTAS_DE_LECTURA)


# --- escritura: solo_lectura, sin almacén ni Slack ---
@pytest.mark.parametrize("agente", AGENTES)
@pytest.mark.parametrize("herramienta", HERRAMIENTAS_DE_ESCRITURA)
async def test_hermes_no_escribe(cliente_http, board, herramienta, agente):
    almacen, slack = board
    # Un traspaso real (Codex → Claude) para que el acuse tenga a qué referirse.
    _, es_error, t = await llamar(cliente_http, CODEX, "registrar_traspaso", TRASPASO_CODEX)
    assert not es_error, t
    antes, peticiones_slack = _foto(almacen), len(slack.peticiones)

    estado, es_error, datos = await llamar(cliente_http, HERMES, herramienta, _argumentos(herramienta, agente, t["traspaso_id"]))

    assert estado == 200 and es_error and datos["error"] == "solo_lectura", (estado, datos)
    assert _foto(almacen) == antes
    assert len(slack.peticiones) == peticiones_slack


async def test_hermes_no_firma_como_hermes(cliente_http, board):
    """Hermes no es agente ni destinatario: el nombre no se acepta y nada se escribe."""
    almacen, slack = board
    antes = _foto(almacen)
    for cambios in ({"agente": "Hermes", "entrega_a": "Claude"}, {"agente": "Codex", "entrega_a": "Hermes"}):
        estado, es_error, datos = await llamar(cliente_http, HERMES, "registrar_traspaso", {**TRASPASO_CODEX, **cambios})
        assert estado == 200 and es_error, (estado, datos)
    assert _foto(almacen) == antes
    assert slack.peticiones == []


async def test_hermes_no_es_agente_ni_destinatario(cliente_http, entorno):
    """Control: Hermes no entra en los nombres que firman ni en los que reciben."""
    assert "hermes" not in coordinacion.AGENTE_POR_CLIENTE
    assert "Hermes" not in typing.get_args(coordinacion.AgenteNombre)
    assert "Hermes" not in typing.get_args(coordinacion.Destinatario)
    respuesta = await _tools_list(cliente_http, CODEX)
    assert "Hermes" not in respuesta.text


# --- tokens repetidos ---
@pytest.mark.parametrize("otra", [v for v in TOKENS if v != "MCP_TOKEN_HERMES"])
@pytest.mark.parametrize("relleno", ["", "  "])
async def test_hermes_repetido_con_otra_variable_da_503(cliente_http, entorno, monkeypatch, caplog, otra, relleno):
    valor = TOKENS[otra]
    monkeypatch.setenv("MCP_TOKEN_HERMES", f"{relleno}{valor}{relleno}")
    caplog.set_level(logging.DEBUG)
    for token in (valor, CODEX if otra != "MCP_TOKEN_CODEX" else OPENAI):
        respuesta = await _tools_list(cliente_http, token)
        assert respuesta.status_code == 503
        assert respuesta.json()["error"] == "configuracion_invalida"
        assert valor not in respuesta.text
    assert "MCP_TOKEN_HERMES" in caplog.text and otra in caplog.text
    assert valor not in caplog.text


# --- sin la variable, nada cambia ---
@pytest.mark.parametrize("ausente", [None, "", "   "])
async def test_sin_token_de_hermes_nada_cambia(cliente_http, board, monkeypatch, ausente):
    """Control: sin MCP_TOKEN_HERMES (o vacía), su valor no autentica y los demás siguen igual."""
    almacen, slack = board
    if ausente is None:
        monkeypatch.delenv("MCP_TOKEN_HERMES", raising=False)
    else:
        monkeypatch.setenv("MCP_TOKEN_HERMES", ausente)
    estado, _, _ = await llamar(cliente_http, HERMES, "obtener_encargo_vigente")
    assert estado == 401
    assert (await _tools_list(cliente_http, CODEX)).status_code == 200
    _, es_error, datos = await llamar(cliente_http, OPENAI, "registrar_traspaso", TRASPASO_CODEX)
    assert es_error and datos["error"] == "solo_lectura"
    _, es_error, t = await llamar(cliente_http, CODEX, "registrar_traspaso", TRASPASO_CODEX)
    assert not es_error, t
    assert len(slack.mensajes) == 1
