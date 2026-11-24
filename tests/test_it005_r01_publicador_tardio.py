"""IT-005 · IT005-R01: un publicador tardío no duplica la operación.

A anota su operación en el encargo (fase 1: el compare-and-set se escribe), pero la respuesta
del almacén le llega tarde. Mientras tanto, la operación pudo vencer y otro (B con la misma
clave, u otro agente) pudo concluir «vencida y ausente de Slack», descartarla y publicar.

Propiedad: una operación nunca produce más de un mensaje y no se publica nada de una operación
que otro pudo haber descartado. Lo peor aceptable es que falte un aviso.

La barrera «aplicado» detiene a A justo después de que el almacén escribió y antes de que la
respuesta le llegue; mientras está detenida, el reloj simulado se adelanta.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from conftest import ENCARGO_INICIAL, Barrera, anota_operacion, como, escribe_traspaso

import coordinacion

pytestmark = pytest.mark.anyio

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
CLAVE_A = "Codex:codex-ig01-v2-0001"

# Último segundo (contado desde que se anota la operación) en que el emisor todavía puede empezar a
# publicar: la publicación (tope de 30 s) termina al menos otros 30 s antes de que la operación venza
# a los 120 s. Se calcula con los valores vigentes, que no cambian.
LIMITE = coordinacion.SEGUNDOS_OPERACION - 2 * coordinacion.SEGUNDOS_PLAZO_SLACK


async def _desenlace(tarea):
    """El resultado de la tarea, o el ErrorBoard con que terminó."""
    try:
        return await tarea
    except coordinacion.ErrorBoard as error:
        return error


async def _a_detenida_tras_anotar(almacen):
    """Lanza a A (traspaso de Codex) y la detiene con su operación ya escrita en el encargo."""
    barrera = Barrera("A", "aplicado", anota_operacion)
    almacen.ganchos.append(barrera)
    a = asyncio.create_task(como("codex", coordinacion.registrar_traspaso, **TRASPASO), name="A")
    await barrera.esperar()
    operacion = almacen.encargo["operacion_en_curso"]
    assert operacion["agente"] == "Codex" and almacen.idem[CLAVE_A]["estado"] == "registrado"
    return a, barrera, operacion["id"]


async def test_respuesta_tardia_y_reintento_con_la_misma_clave_publican_una_sola_vez(board, reloj):
    """La prueba mínima de la tarea: A escribe el registro, su respuesta llega después del
    vencimiento, B reintenta con la misma clave y después A continúa."""
    almacen, slack = board
    a, barrera, operacion_a = await _a_detenida_tras_anotar(almacen)
    reloj.avanzar(coordinacion.SEGUNDOS_OPERACION + 1)
    b = await como("codex", coordinacion.registrar_traspaso, **TRASPASO)
    barrera.soltar()
    desenlace_a = await _desenlace(a)

    # Un solo mensaje: el de B. A no envió nada a Slack.
    assert len(slack.mensajes) == 1, [m["metadata"]["event_payload"]["id"] for m in slack.mensajes]
    assert len(slack.publicados) == 1
    assert slack.publicados[0]["metadata"]["event_payload"]["id"] == b["traspaso_id"] != operacion_a
    # Estado final coherente: el traspaso de B, sin operación pendiente, listado una vez.
    assert almacen.encargo["version"] == "v2" and almacen.encargo["responsable_siguiente"] == "Claude"
    assert "operacion_en_curso" not in almacen.encargo
    assert almacen.encargo["traspaso_vigente"] == b["traspaso_id"]
    assert almacen.orden == [b["traspaso_id"]] and set(almacen.traspasos) == {b["traspaso_id"]}
    assert almacen.traspasos[b["traspaso_id"]]["slack_ts"] == slack.publicados[0]["ts"]
    assert almacen.idem[CLAVE_A]["estado"] == "completado"
    # La respuesta de A no engaña: no presenta como propio un éxito ni el resultado de B.
    assert isinstance(desenlace_a, coordinacion.ErrorBoard), desenlace_a
    assert desenlace_a.codigo == "operacion_vencida"
    assert operacion_a in desenlace_a.mensaje
    # Un reintento posterior devuelve lo que quedó registrado, sin publicar de nuevo.
    otra = await como("codex", coordinacion.registrar_traspaso, **TRASPASO)
    assert otra["duplicado"] is True and otra["traspaso_id"] == b["traspaso_id"]
    assert len(slack.mensajes) == 1


async def test_respuesta_tardia_no_publica_la_operacion_que_otro_agente_descarto(board, reloj):
    """Otro agente (Fabián) encuentra vencida la operación de A, la concilia (no está en Slack), la
    descarta y escribe la suya. Después A continúa: no debe publicar su traspaso descartado."""
    almacen, slack = board
    a, barrera, operacion_a = await _a_detenida_tras_anotar(almacen)
    reloj.avanzar(coordinacion.SEGUNDOS_OPERACION + 1)
    fabian = await como("propietario", coordinacion.registrar_traspaso, **DEL_PROPIETARIO)
    barrera.soltar()
    desenlace_a = await _desenlace(a)

    assert len(slack.mensajes) == 1, [m["metadata"]["event_payload"]["de"] for m in slack.mensajes]
    assert slack.publicados[0]["metadata"]["event_payload"]["de"] == "Fabián"
    assert almacen.encargo["responsable_siguiente"] == "Muse"
    assert almacen.encargo["traspaso_vigente"] == fabian["traspaso_id"]
    assert "operacion_en_curso" not in almacen.encargo
    assert set(almacen.traspasos) == {fabian["traspaso_id"]}
    assert CLAVE_A not in almacen.idem  # la descartó Fabián: la clave de Codex quedó libre
    assert isinstance(desenlace_a, coordinacion.ErrorBoard), desenlace_a
    assert desenlace_a.codigo == "operacion_vencida"
    assert operacion_a in desenlace_a.mensaje


@pytest.mark.parametrize("retraso", [LIMITE + 1, coordinacion.SEGUNDOS_OPERACION + 1], ids=["pasado_el_limite", "vencida"])
async def test_respuesta_tardia_sin_nadie_mas_no_publica_y_deja_la_clave_libre(board, reloj, retraso):
    """Nadie más intervino, pero la respuesta llegó cuando ya no se puede publicar a tiempo: A no
    publica, deshace su propia operación y el reintento con la misma clave publica una sola vez."""
    almacen, slack = board
    a, barrera, operacion_a = await _a_detenida_tras_anotar(almacen)
    reloj.avanzar(retraso)
    barrera.soltar()
    desenlace_a = await _desenlace(a)

    assert slack.mensajes == []
    assert isinstance(desenlace_a, coordinacion.ErrorBoard), desenlace_a
    assert desenlace_a.codigo == "operacion_vencida"
    assert operacion_a in desenlace_a.mensaje
    assert almacen.encargo == json.loads(json.dumps(ENCARGO_INICIAL))
    assert CLAVE_A not in almacen.idem
    resultado = await como("codex", coordinacion.registrar_traspaso, **TRASPASO)
    assert resultado["duplicado"] is False
    assert len(slack.mensajes) == 1 and almacen.encargo["version"] == "v2"


async def test_control_respuesta_dentro_del_limite_publica_normalmente(board, reloj):
    """CONTROL (pasa antes y después de la corrección): con la respuesta justo en el límite, A
    todavía publica a tiempo y completa con un solo mensaje."""
    almacen, slack = board
    a, barrera, operacion_a = await _a_detenida_tras_anotar(almacen)
    reloj.avanzar(LIMITE)
    barrera.soltar()
    resultado = await a
    assert resultado["duplicado"] is False and resultado["traspaso_id"] == operacion_a
    assert len(slack.mensajes) == 1
    assert almacen.encargo["version"] == "v2" and "operacion_en_curso" not in almacen.encargo


async def test_acuse_con_respuesta_tardia_y_reintento_responde_una_sola_vez(board, reloj):
    """El mismo caso con el acuse, que publica en el hilo del traspaso."""
    almacen, slack = board
    t = await como("codex", coordinacion.registrar_traspaso, **TRASPASO)
    acuse = {"agente": "Claude", "traspaso_id": t["traspaso_id"], "clave_idempotencia": "claude-acuse-0001", "confirmar": True}
    barrera = Barrera("A", "aplicado", anota_operacion)
    almacen.ganchos.append(barrera)
    a = asyncio.create_task(como("principal", coordinacion.registrar_acuse, **acuse), name="A")
    await barrera.esperar()
    acuse_a = almacen.encargo["operacion_en_curso"]["id"]
    reloj.avanzar(coordinacion.SEGUNDOS_OPERACION + 1)
    b = await como("principal", coordinacion.registrar_acuse, **acuse)
    barrera.soltar()
    desenlace_a = await _desenlace(a)

    respuestas = [m for m in slack.mensajes if m.get("thread_ts")]
    assert len(respuestas) == 1, [m["metadata"]["event_payload"]["id"] for m in respuestas]
    assert respuestas[0]["metadata"]["event_payload"]["id"] != acuse_a
    assert b["duplicado"] is False
    assert almacen.encargo["estado"] == "en_curso" and "operacion_en_curso" not in almacen.encargo
    assert almacen.traspasos[t["traspaso_id"]]["acuse"]["id"] == respuestas[0]["metadata"]["event_payload"]["id"]
    assert isinstance(desenlace_a, coordinacion.ErrorBoard), desenlace_a
    assert desenlace_a.codigo == "operacion_vencida"


async def test_el_publicador_no_presenta_como_suyo_el_resultado_de_otra_operacion(board, reloj):
    """Caso residual, fuera de los supuestos: A publicó a tiempo, pero el historial de Slack todavía
    no mostraba su mensaje cuando B (misma clave) concilió la operación vencida, así que B la
    descartó y publicó la suya. Ese duplicado es el riesgo residual documentado (historial
    consistente). Lo que se prueba aquí es solo la respuesta de A: no debe devolver el resultado
    de la operación de B como si fuera la suya."""
    almacen, slack = board
    barrera = Barrera("A", "aplicar", escribe_traspaso)
    almacen.ganchos.append(barrera)
    a = asyncio.create_task(como("codex", coordinacion.registrar_traspaso, **TRASPASO), name="A")
    await barrera.esperar()  # A ya publicó y está por completar
    operacion_a = almacen.encargo["operacion_en_curso"]["id"]
    mensaje_a = slack.publicados.pop()  # el historial todavía no lo muestra
    reloj.avanzar(coordinacion.SEGUNDOS_OPERACION + 1)
    b = await como("codex", coordinacion.registrar_traspaso, **TRASPASO)
    slack.publicados.insert(0, mensaje_a)
    barrera.soltar()
    desenlace_a = await _desenlace(a)

    assert b["traspaso_id"] != operacion_a
    assert not isinstance(desenlace_a, dict), f"A devolvió el resultado de otra operación: {desenlace_a}"
    assert desenlace_a.codigo == "registro_incompleto"
    assert operacion_a in desenlace_a.mensaje
