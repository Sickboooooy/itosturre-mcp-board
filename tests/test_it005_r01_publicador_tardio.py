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
from conftest import ENCARGO_INICIAL, Barrera, anota_marca_de_publicacion, anota_operacion, como, escribe_traspaso

import almacen as modulo_almacen
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


async def test_historial_atrasado_no_autoriza_otro_envio(board, reloj):
    """IT005-R01, segunda ronda. Antes se llamaba
    test_el_publicador_no_presenta_como_suyo_el_resultado_de_otra_operacion y aceptaba el duplicado
    como «riesgo residual»; ahora exige un solo mensaje.

    A publicó a tiempo y quedó detenida antes de completar. El historial de Slack todavía no muestra
    su mensaje cuando B (misma clave) concilia la operación vencida. «No aparece en el historial» no
    prueba que el mensaje no salió: como A ya había anotado que iba a publicar, B no puede
    descartarla ni publicar otra; la aplica sin volver a publicar (no_confirmada)."""
    almacen, slack = board
    barrera = Barrera("A", "aplicar", escribe_traspaso)
    almacen.ganchos.append(barrera)
    a = asyncio.create_task(como("codex", coordinacion.registrar_traspaso, **TRASPASO), name="A")
    await barrera.esperar()  # A ya publicó y está por completar
    operacion_a = almacen.encargo["operacion_en_curso"]["id"]
    mensaje_a = slack.publicados.pop()  # el historial todavía no lo muestra
    reloj.avanzar(coordinacion.SEGUNDOS_OPERACION + 1)
    desenlace_b = await _desenlace(como("codex", coordinacion.registrar_traspaso, **TRASPASO))
    slack.publicados.insert(0, mensaje_a)  # el historial se pone al día
    barrera.soltar()
    desenlace_a = await _desenlace(a)

    # Un solo mensaje para la operación: B no envió nada a Slack.
    assert len(slack.mensajes) == 1, [m["metadata"]["event_payload"]["id"] for m in slack.mensajes]
    assert slack.publicados == [mensaje_a]
    # Estado final coherente: la operación de A, aplicada una sola vez, sin confirmar su publicación.
    assert almacen.encargo["version"] == "v2" and almacen.encargo["traspaso_vigente"] == operacion_a
    assert "operacion_en_curso" not in almacen.encargo
    assert set(almacen.traspasos) == {operacion_a} and almacen.orden == [operacion_a]
    assert almacen.traspasos[operacion_a]["publicacion"]["estado"] == "no_confirmada"
    assert almacen.idem[CLAVE_A]["estado"] == "completado" and almacen.idem[CLAVE_A]["evento_id"] == operacion_a
    # Respuestas que no engañan: B recibe la operación de A como ya registrada; A, su propia operación.
    assert isinstance(desenlace_b, dict), desenlace_b
    assert desenlace_b["duplicado"] is True and desenlace_b["traspaso_id"] == operacion_a
    assert isinstance(desenlace_a, dict), desenlace_a
    assert desenlace_a["traspaso_id"] == operacion_a


# --- IT005-R01, segunda ronda: la marca de publicación ---
# El emisor anota con un compare-and-set, justo antes de llamar a Slack, que va a publicar
# (`publicacion_iniciada_en`) y solo publica si esa anotación entra a tiempo. Una operación vencida
# sin marca se descarta: su emisor no llamó a Slack ni lo hará. Con marca, nunca se descarta: se
# completa sin volver a publicar (no_confirmada), aunque falte el aviso.


async def _a_muere_antes_de_marcar(almacen) -> str:
    """A anota su operación (fase 1) y muere antes de anotar la marca de publicación."""
    barrera = Barrera("A", "aplicado", anota_operacion)
    almacen.ganchos.append(barrera)
    a = asyncio.create_task(como("codex", coordinacion.registrar_traspaso, **TRASPASO), name="A")
    await barrera.esperar()
    a.cancel()
    with pytest.raises(asyncio.CancelledError):
        await a
    operacion = almacen.encargo["operacion_en_curso"]
    assert operacion["agente"] == "Codex" and "publicacion_iniciada_en" not in operacion
    return operacion["id"]


async def test_control_emisor_que_muere_antes_de_marcar_se_descarta_y_el_encargo_sigue(board, reloj):
    """CONTROL (pasa antes y después): la operación vencida sin marca se puede descartar. Otro agente
    la descarta y escribe la suya; la clave de A queda libre. (Son las comprobaciones que antes hacía
    test_it005_slack.py::test_proceso_que_muere_antes_de_publicar_no_bloquea_el_encargo, que ahora
    cubre la muerte después de la marca.)"""
    almacen, slack = board
    await _a_muere_antes_de_marcar(almacen)
    with pytest.raises(coordinacion.ErrorBoard) as error:
        await como("propietario", coordinacion.registrar_traspaso, **DEL_PROPIETARIO)
    assert error.value.codigo == "operacion_en_curso"
    reloj.avanzar(coordinacion.SEGUNDOS_OPERACION + 1)
    resultado = await como("propietario", coordinacion.registrar_traspaso, **DEL_PROPIETARIO)
    assert resultado["encargo"]["responsable_siguiente"] == "Muse"
    assert len(slack.mensajes) == 1 and slack.mensajes[0]["metadata"]["event_payload"]["de"] == "Fabián"
    assert CLAVE_A not in almacen.idem
    with pytest.raises(coordinacion.ErrorBoard) as error:
        await como("codex", coordinacion.registrar_traspaso, **TRASPASO)
    assert error.value.codigo == "no_es_responsable"
    assert len(slack.mensajes) == 1


async def test_sin_marca_se_descarta_aunque_no_se_pueda_consultar_slack(board, reloj):
    """Sin marca, descartar no depende del historial: se descarta aunque no se pueda consultar Slack, y
    el reintento publica una sola vez. Antes se aplicaba sin aviso (no_confirmada)."""
    almacen, slack = board
    slack.historial_disponible = False
    operacion_a = await _a_muere_antes_de_marcar(almacen)
    reloj.avanzar(coordinacion.SEGUNDOS_OPERACION + 1)
    resultado = await como("codex", coordinacion.registrar_traspaso, **TRASPASO)
    assert resultado["duplicado"] is False and resultado["traspaso_id"] != operacion_a
    assert len(slack.mensajes) == 1
    assert set(almacen.traspasos) == {resultado["traspaso_id"]}
    assert almacen.traspasos[resultado["traspaso_id"]]["publicacion"]["estado"] == "publicado"


async def test_marca_que_entra_tarde_no_publica_y_el_emisor_la_deshace(board, reloj):
    """La marca se escribió, pero su confirmación le llega a A pasado el límite para publicar: A no
    publica. Como solo A podía publicar esta operación y no lo hizo, la deshace (compare-and-set) y la
    clave queda libre; el reintento publica una sola vez."""
    almacen, slack = board
    barrera = Barrera("A", "aplicado", anota_marca_de_publicacion)
    almacen.ganchos.append(barrera)
    a = asyncio.create_task(como("codex", coordinacion.registrar_traspaso, **TRASPASO), name="A")
    await barrera.esperar()
    operacion_a = almacen.encargo["operacion_en_curso"]["id"]
    reloj.avanzar(LIMITE + 1)
    barrera.soltar()
    desenlace_a = await _desenlace(a)
    assert slack.mensajes == []
    assert isinstance(desenlace_a, coordinacion.ErrorBoard), desenlace_a
    assert desenlace_a.codigo == "operacion_vencida" and operacion_a in desenlace_a.mensaje
    assert almacen.encargo == json.loads(json.dumps(ENCARGO_INICIAL))
    assert CLAVE_A not in almacen.idem
    resultado = await como("codex", coordinacion.registrar_traspaso, **TRASPASO)
    assert resultado["duplicado"] is False and len(slack.mensajes) == 1


async def test_rechazo_de_slack_sin_poder_deshacer_no_dice_que_no_se_registro(board, reloj):
    """CASO SIN AVISO: Slack rechaza el mensaje de forma definitiva, pero el almacén falla al deshacer
    el registro. La operación conserva la marca, así que al vencer se aplicará sin aviso: la respuesta
    no puede decir «no se registró»."""
    almacen, slack = board
    slack.modo = "error"  # not_in_channel; el historial tampoco se puede consultar

    async def falla_al_deshacer(momento, *datos):
        if momento == "aplicar":
            _, escrituras = datos
            if any(e[0] == "borrar" for e in escrituras) and any(e[0] == "poner" and e[1] == ("encargo",) for e in escrituras):
                raise modulo_almacen.ErrorAlmacen()

    almacen.ganchos.append(falla_al_deshacer)
    with pytest.raises(coordinacion.ErrorBoard) as error:
        await como("codex", coordinacion.registrar_traspaso, **TRASPASO)
    assert error.value.codigo == "registro_incompleto", error.value.mensaje
    assert "al vencer se aplicará sin aviso" in error.value.mensaje
    almacen.ganchos.remove(falla_al_deshacer)
    operacion_a = almacen.encargo["operacion_en_curso"]
    assert operacion_a.get("publicacion_iniciada_en")
    reloj.avanzar(coordinacion.SEGUNDOS_OPERACION + 1)
    resultado = await como("codex", coordinacion.registrar_traspaso, **TRASPASO)
    assert resultado["duplicado"] is True and resultado["traspaso_id"] == operacion_a["id"]
    assert len(slack.mensajes) == 1 and slack.publicados == []
    assert almacen.traspasos[operacion_a["id"]]["publicacion"]["estado"] == "no_confirmada"


async def test_marca_sin_respuesta_no_publica_y_al_vencer_se_aplica_sin_aviso(board, reloj):
    """CASO SIN AVISO: la marca entró, pero su respuesta se perdió. A no sabe si quedó anotada, así que
    no publica. Como la marca sí está, nadie puede descartar la operación: al vencer se aplica sin
    publicar (no_confirmada). Falta el aviso; no hay duplicado."""
    almacen, slack = board
    almacen.perder_respuesta = lambda escrituras: anota_marca_de_publicacion(None, escrituras)
    with pytest.raises(coordinacion.ErrorBoard) as error:
        await como("codex", coordinacion.registrar_traspaso, **TRASPASO)
    assert error.value.codigo == "registro_incompleto"
    almacen.perder_respuesta = None
    operacion_a = almacen.encargo["operacion_en_curso"]
    assert operacion_a.get("publicacion_iniciada_en")
    assert slack.mensajes == []
    with pytest.raises(coordinacion.ErrorBoard) as error:
        await como("codex", coordinacion.registrar_traspaso, **TRASPASO)
    assert error.value.codigo == "en_proceso"
    reloj.avanzar(coordinacion.SEGUNDOS_OPERACION + 1)
    resultado = await como("codex", coordinacion.registrar_traspaso, **TRASPASO)
    assert resultado["duplicado"] is True and resultado["traspaso_id"] == operacion_a["id"]
    assert slack.mensajes == []
    assert almacen.encargo["version"] == "v2" and "operacion_en_curso" not in almacen.encargo
    assert almacen.traspasos[operacion_a["id"]]["publicacion"]["estado"] == "no_confirmada"
