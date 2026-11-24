"""IT-005 · Brecha 10: vencimiento y recuperación de trabajo.

El responsable tiene un arriendo con `vence_en`: lo recibe con el traspaso, se renueva con el
acuse y con renovar_arriendo, y al vencer deja de poder escribir. Un encargo vencido lo puede
recuperar cualquier agente con firma (también el titular). Fabián no tiene arriendo: su revisión
no se puede saltar esperando a que venza. El reloj es simulado (conftest.reloj).
"""

from __future__ import annotations

import asyncio

import pytest
from conftest import Barrera, TOKENS, como, escribe_encargo, llamar

import coordinacion

pytestmark = pytest.mark.anyio

CODEX = TOKENS["MCP_TOKEN_CODEX"]
MUSE = TOKENS["MCP_TOKEN_MUSE"]

CUATRO_HORAS = 4 * 3600
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


def _acuse(traspaso_id: str, clave: str = "claude-acuse-0001") -> dict:
    return {"agente": "Claude", "traspaso_id": traspaso_id, "clave_idempotencia": clave, "confirmar": True}


def _renovar(agente: str, clave: str) -> dict:
    return {"agente": agente, "encargo_id": "IG-01", "clave_idempotencia": clave, "confirmar": True}


async def _entregado_y_acusado(reloj) -> str:
    """Codex entrega v2 a Claude a las 12:00 y Claude acusa a la misma hora."""
    t = await como("codex", coordinacion.registrar_traspaso, **TRASPASO)
    await como("principal", coordinacion.registrar_acuse, **_acuse(t["traspaso_id"]))
    return t["traspaso_id"]


async def _error(cliente: str, herramienta, **argumentos) -> str:
    with pytest.raises(coordinacion.ErrorBoard) as error:
        await como(cliente, herramienta, **argumentos)
    return error.value.codigo


# --- el arriendo y su vencimiento ---
async def test_el_traspaso_da_un_arriendo_al_destinatario(board, reloj):
    resultado = await como("codex", coordinacion.registrar_traspaso, **TRASPASO)
    assert resultado["encargo"]["arriendo"] == {"titular": "Claude", "desde": "2026-11-23T12:00:00Z", "vence_en": "2026-11-23T16:00:00Z"}


async def test_el_acuse_renueva_el_arriendo(board, reloj):
    almacen, _ = board
    t = await como("codex", coordinacion.registrar_traspaso, **TRASPASO)
    reloj.avanzar(3600)
    await como("principal", coordinacion.registrar_acuse, **_acuse(t["traspaso_id"]))
    assert almacen.encargo["arriendo"] == {"titular": "Claude", "desde": "2026-11-23T13:00:00Z", "vence_en": "2026-11-23T17:00:00Z"}


async def test_un_traspaso_a_fabian_no_tiene_arriendo(board, reloj):
    resultado = await como("codex", coordinacion.registrar_traspaso, **{**TRASPASO, "entrega_a": "Fabián"})
    assert resultado["encargo"]["arriendo"] is None


async def test_renovar_extiende_el_arriendo_sin_publicar(board, reloj):
    almacen, slack = board
    await _entregado_y_acusado(reloj)
    reloj.avanzar(3 * 3600)
    resultado = await como("principal", coordinacion.renovar_arriendo, **_renovar("Claude", "claude-renueva-0001"))
    assert resultado["arriendo"]["vence_en"] == "2026-11-23T19:00:00Z"
    assert resultado["rev"] == 3 and almacen.encargo["rev"] == 3
    assert len(slack.publicados) == 2  # el traspaso y el acuse; renovar no publica


async def test_solo_el_responsable_renueva(board, reloj):
    await _entregado_y_acusado(reloj)
    assert await _error("muse", coordinacion.renovar_arriendo, **_renovar("Muse", "muse-renueva-0001")) == "no_es_responsable"


async def test_un_arriendo_vencido_deja_sin_escritura_al_titular(cliente_http, board, reloj):
    almacen, slack = board
    await _entregado_y_acusado(reloj)
    reloj.avanzar(CUATRO_HORAS)
    traspaso_v3 = {**TRASPASO, "agente": "Claude", "version": "v3", "entrega_a": "Muse", "clave_idempotencia": "claude-ig01-v3-0001"}
    assert await _error("principal", coordinacion.registrar_traspaso, **traspaso_v3) == "arriendo_vencido"
    assert await _error("principal", coordinacion.renovar_arriendo, **_renovar("Claude", "claude-renueva-0001")) == "arriendo_vencido"
    _, _, encargo = await llamar(cliente_http, MUSE, "obtener_encargo_vigente")
    assert encargo["arriendo_vencido"] is True
    assert almacen.encargo["responsable_siguiente"] == "Claude" and len(slack.publicados) == 2


async def test_un_segundo_antes_de_vencer_todavia_escribe(board, reloj):
    await _entregado_y_acusado(reloj)
    reloj.avanzar(CUATRO_HORAS - 1)
    resultado = await como("principal", coordinacion.renovar_arriendo, **_renovar("Claude", "claude-renueva-0001"))
    assert resultado["arriendo"]["vence_en"] == "2026-11-23T19:59:59Z"


# --- recuperación ---
async def test_otro_agente_recupera_un_encargo_vencido(board, reloj):
    almacen, _ = board
    await _entregado_y_acusado(reloj)
    reloj.avanzar(CUATRO_HORAS + 1)
    resultado = await como(
        "muse", coordinacion.recuperar_encargo, **_renovar("Muse", "muse-recupera-0001"), nota="Claude no respondió; sigo yo."
    )
    assert resultado["recuperado_de"] == "Claude" and resultado["duplicado"] is False
    encargo = almacen.encargo
    assert encargo["responsable_siguiente"] == "Muse" and encargo["estado"] == "en_curso"
    assert encargo["arriendo"] == {"titular": "Muse", "desde": "2026-11-23T16:00:01Z", "vence_en": "2026-11-23T20:00:01Z"}
    assert encargo["rev"] == 3
    assert encargo["ultima_recuperacion"]["de"] == "Claude" and encargo["ultima_recuperacion"]["nota"] == "Claude no respondió; sigo yo."
    # El nuevo responsable escribe; el anterior ya no.
    traspaso_muse = {**TRASPASO, "agente": "Muse", "version": "v3", "entrega_a": "Codex", "clave_idempotencia": "muse-ig01-v3-0001"}
    assert (await como("muse", coordinacion.registrar_traspaso, **traspaso_muse))["encargo"]["responsable_siguiente"] == "Codex"
    traspaso_claude = {**TRASPASO, "agente": "Claude", "version": "v4", "entrega_a": "Muse", "clave_idempotencia": "claude-ig01-v4-0001"}
    assert await _error("principal", coordinacion.registrar_traspaso, **traspaso_claude) == "no_es_responsable"


async def test_no_se_recupera_antes_de_vencer(board, reloj):
    await _entregado_y_acusado(reloj)
    reloj.avanzar(CUATRO_HORAS - 1)
    assert await _error("muse", coordinacion.recuperar_encargo, **_renovar("Muse", "muse-recupera-0001")) == "arriendo_vigente"


async def test_la_revision_de_fabian_no_se_puede_saltar(board, reloj):
    await como("codex", coordinacion.registrar_traspaso, **{**TRASPASO, "entrega_a": "Fabián"})
    reloj.avanzar(10 * CUATRO_HORAS)
    assert await _error("muse", coordinacion.recuperar_encargo, **_renovar("Muse", "muse-recupera-0001")) == "sin_arriendo"


async def test_un_traspaso_sin_acuse_vencido_se_recupera_y_el_acuse_tardio_no_vale(board, reloj):
    almacen, slack = board
    t = await como("codex", coordinacion.registrar_traspaso, **TRASPASO)
    reloj.avanzar(CUATRO_HORAS + 1)
    assert await _error("principal", coordinacion.registrar_acuse, **_acuse(t["traspaso_id"])) == "arriendo_vencido"
    await como("codex", coordinacion.recuperar_encargo, **_renovar("Codex", "codex-recupera-0001"))
    assert almacen.encargo["responsable_siguiente"] == "Codex"
    assert await _error("principal", coordinacion.registrar_acuse, **_acuse(t["traspaso_id"], "claude-acuse-0002")) == "traspaso_no_vigente"
    assert len(slack.publicados) == 1


async def test_el_titular_puede_recuperar_su_propio_encargo_vencido(board, reloj):
    almacen, _ = board
    await _entregado_y_acusado(reloj)
    reloj.avanzar(CUATRO_HORAS + 1)
    await como("principal", coordinacion.recuperar_encargo, **_renovar("Claude", "claude-recupera-0001"))
    assert almacen.encargo["arriendo"]["titular"] == "Claude" and almacen.encargo["arriendo"]["vence_en"] == "2026-11-23T20:00:01Z"


async def test_recuperar_y_renovar_son_idempotentes(board, reloj):
    almacen, _ = board
    await _entregado_y_acusado(reloj)
    reloj.avanzar(CUATRO_HORAS + 1)
    primero = await como("muse", coordinacion.recuperar_encargo, **_renovar("Muse", "muse-recupera-0001"))
    repetido = await como("muse", coordinacion.recuperar_encargo, **_renovar("Muse", "muse-recupera-0001"))
    assert repetido["duplicado"] is True and repetido["encargo"] == primero["encargo"]
    assert almacen.encargo["rev"] == 3
    reloj.avanzar(60)
    renovado = await como("muse", coordinacion.renovar_arriendo, **_renovar("Muse", "muse-renueva-0001"))
    otra_vez = await como("muse", coordinacion.renovar_arriendo, **_renovar("Muse", "muse-renueva-0001"))
    assert otra_vez["duplicado"] is True and otra_vez["arriendo"] == renovado["arriendo"]
    assert almacen.encargo["rev"] == 4


async def test_dos_recuperaciones_simultaneas_dejan_un_solo_responsable(board, reloj):
    almacen, _ = board
    await _entregado_y_acusado(reloj)
    reloj.avanzar(CUATRO_HORAS + 1)
    barrera = Barrera("A", "aplicar", escribe_encargo)
    almacen.ganchos.append(barrera)
    a = asyncio.create_task(como("muse", coordinacion.recuperar_encargo, **_renovar("Muse", "muse-recupera-0001")), name="A")
    await barrera.esperar()
    await como("codex", coordinacion.recuperar_encargo, **_renovar("Codex", "codex-recupera-0001"))
    barrera.soltar()
    with pytest.raises(coordinacion.ErrorBoard) as error:
        await a
    assert error.value.codigo == "arriendo_vigente"
    assert almacen.encargo["responsable_siguiente"] == "Codex" and almacen.encargo["arriendo"]["titular"] == "Codex"
    assert almacen.encargo["rev"] == 3


async def test_renovar_por_http_crea_el_arriendo_de_un_encargo_cargado_a_mano(cliente_http, board):
    almacen, _ = board
    _, es_error, datos = await llamar(cliente_http, CODEX, "renovar_arriendo", _renovar("Codex", "codex-renueva-0001"))
    assert not es_error, datos
    assert almacen.encargo["arriendo"]["titular"] == "Codex"
    _, es_error, datos = await llamar(cliente_http, MUSE, "recuperar_encargo", _renovar("Muse", "muse-recupera-0001"))
    assert es_error and datos["error"] == "arriendo_vigente"
