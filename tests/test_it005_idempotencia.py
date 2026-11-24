"""IT-005 · Idempotencia ligada a la operación y a los argumentos (riesgo 4) y reserva con propietario,
liberación condicionada, renovación y recuperación (riesgo 9).

Las carreras usan barreras en memoria (conftest.Barrera) y el reloj simulado; no hay TTL real.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from conftest import Barrera, TOKENS, como, escribe_encargo, llamar

import coordinacion

pytestmark = pytest.mark.anyio

CLAUDE = TOKENS["MCP_TOKEN"]
CODEX = TOKENS["MCP_TOKEN_CODEX"]

TRASPASO = {
    "agente": "Codex",
    "encargo_id": "IG-01",
    "version": "v2",
    "entrega_a": "Claude",
    "enlace": "https://docs.google.com/document/d/ficticio-v2",
    "nota": "Integré el guion ficticio.",
    "clave_idempotencia": "codex-ig01-v2-0001",
    "confirmar": True,
}


# --- riesgo 4: la clave queda ligada a la operación y a la huella de los argumentos ---
async def test_misma_clave_con_otros_datos_se_rechaza(cliente_http, board):
    almacen, slack = board
    await llamar(cliente_http, CODEX, "registrar_traspaso", TRASPASO)
    _, es_error, datos = await llamar(cliente_http, CODEX, "registrar_traspaso", {**TRASPASO, "nota": "Otra nota."})
    assert es_error and datos["error"] == "clave_reutilizada"
    assert len(slack.publicados) == 1
    assert almacen.encargo["nota"] == "Integré el guion ficticio."
    _, es_error, datos = await llamar(cliente_http, CODEX, "registrar_traspaso", {**TRASPASO, "entrega_a": "Muse"})
    assert es_error and datos["error"] == "clave_reutilizada"


async def test_misma_clave_en_otra_operacion_no_da_un_exito_falso(cliente_http, board):
    """IT002B-05: la clave de un traspaso, reutilizada en un acuse, devolvía el traspaso como éxito."""
    _, slack = board
    await llamar(cliente_http, CODEX, "registrar_traspaso", TRASPASO)
    _, es_error, datos = await llamar(
        cliente_http,
        CODEX,
        "registrar_acuse",
        {"agente": "Codex", "traspaso_id": "T-20261122-000000-abcd", "clave_idempotencia": TRASPASO["clave_idempotencia"], "confirmar": True},
    )
    assert es_error and datos["error"] == "clave_reutilizada"
    assert len(slack.publicados) == 1


async def test_clave_guardada_antes_de_it005_no_se_reutiliza(cliente_http, board):
    almacen, slack = board
    almacen.idem["Codex:codex-ig01-v2-0001"] = json.dumps({"traspaso_id": "T-20261120-100000-abcd", "permalink": None})
    _, es_error, datos = await llamar(cliente_http, CODEX, "registrar_traspaso", TRASPASO)
    assert es_error and datos["error"] == "clave_reutilizada"
    assert slack.peticiones == []


async def test_reserva_anterior_a_it005_sigue_en_proceso(cliente_http, board):
    almacen, slack = board
    almacen.idem["Codex:codex-ig01-v2-0001"] = "en_proceso"
    _, es_error, datos = await llamar(cliente_http, CODEX, "registrar_traspaso", TRASPASO)
    assert es_error and datos["error"] == "en_proceso"
    assert slack.peticiones == []


# --- riesgo 9: reserva con propietario, renovable, recuperable y de liberación condicionada ---
async def test_misma_clave_mientras_la_reserva_esta_viva_responde_en_proceso(board, reloj):
    almacen, slack = board
    barrera = Barrera("A", "aplicar", escribe_encargo)
    almacen.ganchos.append(barrera)
    a = asyncio.create_task(como("codex", coordinacion.registrar_traspaso, **TRASPASO), name="A")
    await barrera.esperar()
    with pytest.raises(coordinacion.ErrorBoard) as error:
        await como("codex", coordinacion.registrar_traspaso, **TRASPASO)
    assert error.value.codigo == "en_proceso"
    barrera.soltar()
    assert (await a)["duplicado"] is False
    assert len(slack.publicados) == 1


async def test_reserva_vencida_se_recupera_sin_duplicar(board, reloj):
    """IT002B-04: A se detiene con la reserva tomada, la reserva vence y B (misma clave) la recupera."""
    almacen, slack = board
    barrera = Barrera("A", "aplicar", escribe_encargo)
    almacen.ganchos.append(barrera)
    a = asyncio.create_task(como("codex", coordinacion.registrar_traspaso, **TRASPASO), name="A")
    await barrera.esperar()
    reloj.avanzar(coordinacion.SEGUNDOS_RESERVA + 1)
    segundo = await como("codex", coordinacion.registrar_traspaso, **TRASPASO)
    barrera.soltar()
    primero = await a
    assert segundo["duplicado"] is False
    assert primero["duplicado"] is True and primero["traspaso_id"] == segundo["traspaso_id"]
    assert len(slack.publicados) == 1
    assert almacen.encargo["version"] == "v2"
    assert almacen.orden == [segundo["traspaso_id"]]


async def test_la_liberacion_no_borra_la_reserva_de_otro(board, reloj):
    """A decide sobre una foto vieja mientras su reserva vence y B la recupera; al fallar A, la
    liberación es condicionada y no borra la reserva viva de B."""
    almacen, slack = board
    almacen.encargo["responsable_siguiente"] = "Muse"
    barrera_a = Barrera("A", "leer", lambda claves: ("encargo",) in claves)
    barrera_b = Barrera("B", "aplicar", escribe_encargo)
    almacen.ganchos += [barrera_a, barrera_b]
    a = asyncio.create_task(como("codex", coordinacion.registrar_traspaso, **TRASPASO), name="A")
    await barrera_a.esperar()  # A tiene la reserva y una foto en la que Codex no es responsable
    reloj.avanzar(coordinacion.SEGUNDOS_RESERVA + 1)
    almacen.encargo["responsable_siguiente"] = "Codex"
    b = asyncio.create_task(como("codex", coordinacion.registrar_traspaso, **TRASPASO), name="B")
    await barrera_b.esperar()  # B recuperó la reserva vencida y está por anotar la operación
    reserva_b = almacen.idem["Codex:codex-ig01-v2-0001"]
    assert reserva_b["estado"] == "en_proceso"
    barrera_a.soltar()
    with pytest.raises(coordinacion.ErrorBoard) as error:
        await a
    assert error.value.codigo == "no_es_responsable"
    assert almacen.idem["Codex:codex-ig01-v2-0001"] == reserva_b
    barrera_b.soltar()
    assert (await b)["duplicado"] is False
    assert len(slack.publicados) == 1


async def test_la_reserva_se_renueva_al_reintentar_tras_un_conflicto(board, reloj):
    """Si el compare-and-set pierde (otro escribió el encargo), la reserva se renueva antes de reintentar."""
    almacen, slack = board
    primera = Barrera("A", "aplicar", escribe_encargo)
    almacen.ganchos.append(primera)
    a = asyncio.create_task(como("codex", coordinacion.registrar_traspaso, **TRASPASO), name="A")
    await primera.esperar()
    vence_antes = almacen.idem["Codex:codex-ig01-v2-0001"]["vence_en"]
    almacen.encargo["titulo"] = "Reel de prueba (ficticio), título corregido a mano"
    reloj.avanzar(100)
    segunda = Barrera("A", "aplicar", escribe_encargo)
    almacen.ganchos.append(segunda)
    primera.soltar()
    await segunda.esperar()
    assert vence_antes == "2026-11-23T12:02:00Z"
    assert almacen.idem["Codex:codex-ig01-v2-0001"]["vence_en"] == "2026-11-23T12:03:40Z"
    segunda.soltar()
    resultado = await a
    assert resultado["encargo"]["titulo"] == "Reel de prueba (ficticio), título corregido a mano"
    assert len(slack.publicados) == 1


async def test_validacion_fallida_libera_la_reserva(cliente_http, board):
    """Un rechazo antes de publicar libera la clave: corregido el problema, la misma clave funciona."""
    almacen, slack = board
    almacen.encargo["responsable_siguiente"] = "Muse"
    _, es_error, datos = await llamar(cliente_http, CODEX, "registrar_traspaso", TRASPASO)
    assert es_error and datos["error"] == "no_es_responsable"
    assert "Codex:codex-ig01-v2-0001" not in almacen.idem
    almacen.encargo["responsable_siguiente"] = "Codex"
    _, es_error, datos = await llamar(cliente_http, CODEX, "registrar_traspaso", TRASPASO)
    assert not es_error and datos["duplicado"] is False
