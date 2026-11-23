"""IT-005 · Riesgo 3: ningún mensaje duplicado en Slack.

La operación se anota en el encargo («operación en curso») antes de publicar. Si el resultado
de Slack es incierto, se concilia buscando el mensaje por sus metadatos; si no se puede saber,
se aplica sin volver a publicar. `slack.publicados` es lo que Slack de verdad muestra.
"""

from __future__ import annotations

import asyncio

import pytest
from conftest import TOKENS, como, llamar

import almacen as modulo_almacen
import coordinacion

pytestmark = pytest.mark.anyio

CLAUDE = TOKENS["MCP_TOKEN"]
CODEX = TOKENS["MCP_TOKEN_CODEX"]
MUSE = TOKENS["MCP_TOKEN_MUSE"]
PROPIETARIO = TOKENS["MCP_TOKEN_PROPIETARIO"]

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
DEL_PROPIETARIO = {**TRASPASO, "agente": "Fabián", "entrega_a": "Muse", "clave_idempotencia": "fabian-ig01-v2-0001"}


async def test_respuesta_perdida_se_concilia_sin_duplicar(cliente_http, board):
    """IT002B-03: Slack aceptó el mensaje pero la respuesta se perdió."""
    almacen, slack = board
    slack.perder_respuesta = 1
    _, es_error, datos = await llamar(cliente_http, CODEX, "registrar_traspaso", TRASPASO)
    assert es_error and datos["error"] == "publicacion_incierta"
    assert almacen.encargo["version"] == "v1"
    _, _, encargo = await llamar(cliente_http, MUSE, "obtener_encargo_vigente")
    assert encargo["operacion_en_curso"]["tipo"] == "traspaso" and encargo["operacion_en_curso"]["agente"] == "Codex"
    _, es_error, datos = await llamar(cliente_http, CODEX, "registrar_traspaso", TRASPASO)
    assert not es_error, datos
    assert datos["duplicado"] is True
    assert len(slack.publicados) == 1
    assert almacen.encargo["version"] == "v2" and "operacion_en_curso" not in almacen.encargo
    guardado = almacen.traspasos[datos["traspaso_id"]]
    assert guardado["slack_ts"] == slack.publicados[0]["ts"]
    assert guardado["publicacion"]["estado"] == "publicado"
    assert datos["permalink"].endswith(slack.publicados[0]["ts"].replace(".", ""))


async def test_nadie_escribe_mientras_hay_una_operacion_pendiente(cliente_http, board):
    almacen, slack = board
    slack.perder_respuesta = 1
    await llamar(cliente_http, CODEX, "registrar_traspaso", TRASPASO)
    _, es_error, datos = await llamar(cliente_http, PROPIETARIO, "registrar_traspaso", DEL_PROPIETARIO)
    assert es_error and datos["error"] == "operacion_en_curso"
    assert len(slack.publicados) == 1
    assert almacen.encargo["operacion_en_curso"]["agente"] == "Codex"


async def test_incierta_y_ausente_al_vencer_se_descarta_y_se_publica_una_vez(board, reloj):
    """La petición salió pero Slack no la guardó: antes de vencer se espera; vencida, se descarta y se reintenta."""
    almacen, slack = board
    slack.cortar_antes_de_guardar = 1
    with pytest.raises(coordinacion.ErrorBoard) as error:
        await como("codex", coordinacion.registrar_traspaso, **TRASPASO)
    assert error.value.codigo == "publicacion_incierta"
    with pytest.raises(coordinacion.ErrorBoard) as error:
        await como("codex", coordinacion.registrar_traspaso, **TRASPASO)
    assert error.value.codigo == "en_proceso"
    assert slack.publicados == []
    reloj.avanzar(coordinacion.SEGUNDOS_OPERACION + 1)
    resultado = await como("codex", coordinacion.registrar_traspaso, **TRASPASO)
    assert resultado["duplicado"] is False
    assert len(slack.publicados) == 1
    assert almacen.encargo["version"] == "v2"
    assert almacen.orden == [resultado["traspaso_id"]]


async def test_incierta_sin_historial_se_aplica_sin_volver_a_publicar(board, reloj):
    """Si no se puede consultar Slack (falta el permiso de historial), al vencer se aplica la transición
    marcada como no confirmada y no se vuelve a publicar: un aviso faltante, nunca uno duplicado."""
    almacen, slack = board
    slack.perder_respuesta = 1
    slack.historial_disponible = False
    with pytest.raises(coordinacion.ErrorBoard):
        await como("codex", coordinacion.registrar_traspaso, **TRASPASO)
    with pytest.raises(coordinacion.ErrorBoard) as error:
        await como("codex", coordinacion.registrar_traspaso, **TRASPASO)
    assert error.value.codigo == "en_proceso"
    reloj.avanzar(coordinacion.SEGUNDOS_OPERACION + 1)
    resultado = await como("codex", coordinacion.registrar_traspaso, **TRASPASO)
    assert resultado["duplicado"] is True
    assert len(slack.publicados) == 1
    assert almacen.encargo["version"] == "v2"
    assert almacen.traspasos[resultado["traspaso_id"]]["publicacion"]["estado"] == "no_confirmada"


async def test_proceso_que_muere_antes_de_publicar_no_bloquea_el_encargo(board, reloj):
    """El proceso muere tras anotar la operación y antes de publicar. Al vencer, otro escritor
    concilia (no hay mensaje), descarta la operación y sigue."""
    almacen, slack = board
    slack.cancelar_publicacion = 1
    muerta = asyncio.create_task(como("codex", coordinacion.registrar_traspaso, **TRASPASO))
    with pytest.raises(asyncio.CancelledError):
        await muerta
    assert almacen.encargo["operacion_en_curso"]["agente"] == "Codex"
    with pytest.raises(coordinacion.ErrorBoard) as error:
        await como("propietario", coordinacion.registrar_traspaso, **DEL_PROPIETARIO)
    assert error.value.codigo == "operacion_en_curso"
    reloj.avanzar(coordinacion.SEGUNDOS_OPERACION + 1)
    resultado = await como("propietario", coordinacion.registrar_traspaso, **DEL_PROPIETARIO)
    assert resultado["encargo"]["responsable_siguiente"] == "Muse"
    assert len(slack.publicados) == 1 and slack.publicados[0]["metadata"]["event_payload"]["de"] == "Fabián"
    # La clave de Codex quedó libre: su reintento se evalúa de nuevo (Codex ya no es responsable).
    with pytest.raises(coordinacion.ErrorBoard) as error:
        await como("codex", coordinacion.registrar_traspaso, **TRASPASO)
    assert error.value.codigo == "no_es_responsable"
    assert len(slack.publicados) == 1


async def test_fallo_al_guardar_tras_publicar_se_concilia_al_reintentar(cliente_http, board):
    almacen, slack = board
    almacen.fallar_al_guardar = True
    _, es_error, datos = await llamar(cliente_http, CODEX, "registrar_traspaso", TRASPASO)
    assert es_error and datos["error"] == "registro_incompleto"
    almacen.fallar_al_guardar = False
    _, es_error, datos = await llamar(cliente_http, CODEX, "registrar_traspaso", TRASPASO)
    assert not es_error, datos
    assert datos["duplicado"] is True
    assert len(slack.publicados) == 1
    _, _, lista = await llamar(cliente_http, MUSE, "listar_traspasos")
    assert [t["id"] for t in lista["traspasos"]] == [datos["traspaso_id"]]


async def test_guardado_hecho_con_respuesta_perdida_no_duplica(cliente_http, board):
    """IT002B-03, tercera variante: Redis guardó el traspaso pero la respuesta no llegó."""
    almacen, slack = board
    almacen.perder_respuesta = lambda escrituras: any(e[0] == "poner" and e[1][0] == "traspaso" for e in escrituras)
    _, es_error, datos = await llamar(cliente_http, CODEX, "registrar_traspaso", TRASPASO)
    assert es_error and datos["error"] == "registro_incompleto"
    almacen.perder_respuesta = None
    _, es_error, datos = await llamar(cliente_http, CODEX, "registrar_traspaso", TRASPASO)
    assert not es_error and datos["duplicado"] is True
    assert len(slack.publicados) == 1
    assert almacen.orden == [datos["traspaso_id"]]


async def test_acuse_con_lectura_fallida_despues_de_publicar_no_duplica(cliente_http, board):
    """IT002B-03, primera variante: falla una lectura después de publicar el acuse."""
    almacen, slack = board
    _, _, t = await llamar(cliente_http, CODEX, "registrar_traspaso", TRASPASO)
    clave_t = modulo_almacen.clave_traspaso(t["traspaso_id"])
    fallos = {"pendientes": 1}

    async def falla_una_lectura(momento, *datos):
        if momento == "leer" and datos[0] == (clave_t,) and almacen.encargo.get("operacion_en_curso") and fallos["pendientes"]:
            fallos["pendientes"] -= 1
            raise modulo_almacen.ErrorAlmacen()

    almacen.ganchos.append(falla_una_lectura)
    args = {"agente": "Claude", "traspaso_id": t["traspaso_id"], "clave_idempotencia": "claude-acuse-0001", "confirmar": True}
    _, es_error, datos = await llamar(cliente_http, CLAUDE, "registrar_acuse", args)
    assert es_error and datos["error"] == "registro_incompleto"
    _, es_error, datos = await llamar(cliente_http, CLAUDE, "registrar_acuse", args)
    assert not es_error and datos["duplicado"] is True
    assert len(slack.publicados) == 2
    assert slack.publicados[1]["thread_ts"] == slack.publicados[0]["ts"]
    assert almacen.encargo["estado"] == "en_curso"
    assert almacen.traspasos[t["traspaso_id"]]["acuse"]["de"] == "Claude"


async def test_otra_peticion_con_la_misma_clave_completa_la_operacion_publicada(board):
    """La primera petición publicó y se detuvo antes de guardar; la segunda (misma clave) la encuentra
    en Slack y la completa; al seguir, la primera devuelve el mismo resultado."""
    from conftest import Barrera, escribe_traspaso

    almacen, slack = board
    barrera = Barrera("A", "aplicar", escribe_traspaso)
    almacen.ganchos.append(barrera)
    a = asyncio.create_task(como("codex", coordinacion.registrar_traspaso, **TRASPASO), name="A")
    await barrera.esperar()
    segundo = await como("codex", coordinacion.registrar_traspaso, **TRASPASO)
    barrera.soltar()
    primero = await a
    assert segundo["duplicado"] is True
    assert primero["traspaso_id"] == segundo["traspaso_id"]
    assert len(slack.publicados) == 1
    assert almacen.orden == [primero["traspaso_id"]]
