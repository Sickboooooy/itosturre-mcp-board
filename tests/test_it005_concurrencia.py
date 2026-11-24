"""IT-005 · Riesgo 2: las lecturas que deciden y la escritura forman una sola operación atómica.

Cada carrera detiene una petición (tarea "A") con una barrera en memoria justo antes de su
compare-and-set, deja que otras escrituras terminen y la suelta. Con datos viejos, A no escribe.
"""

from __future__ import annotations

import asyncio

import pytest
from conftest import Barrera, como, escribe_encargo, escribe_traspaso

import coordinacion

pytestmark = pytest.mark.anyio


def traspaso(**cambios) -> dict:
    base = {
        "agente": "Codex",
        "encargo_id": "IG-01",
        "version": "v2",
        "entrega_a": "Claude",
        "enlace": "https://docs.google.com/document/d/ficticio-v2",
        "nota": "Integré el guion ficticio.",
        "clave_idempotencia": "codex-ig01-v2-000a",
        "confirmar": True,
    }
    base.update(cambios)
    return base


async def test_una_foto_vieja_no_hace_retroceder_la_version(board):
    """IT002B-02 (version_race): A decide v2 sobre v1; mientras, el encargo llega a v3."""
    almacen, slack = board
    barrera = Barrera("A", "aplicar", escribe_encargo)
    almacen.ganchos.append(barrera)
    a = asyncio.create_task(como("codex", coordinacion.registrar_traspaso, **traspaso()), name="A")
    await barrera.esperar()
    await como("codex", coordinacion.registrar_traspaso, **traspaso(clave_idempotencia="codex-ig01-v2-000b"))
    await como(
        "principal",
        coordinacion.registrar_traspaso,
        **traspaso(agente="Claude", version="v3", entrega_a="Muse", clave_idempotencia="claude-ig01-v3-000a"),
    )
    barrera.soltar()
    with pytest.raises(coordinacion.ErrorBoard) as error:
        await a
    assert error.value.codigo == "no_es_responsable"
    assert almacen.encargo["version"] == "v3" and almacen.encargo["responsable_siguiente"] == "Muse"
    assert almacen.encargo["rev"] == 2
    assert len(slack.publicados) == 2
    assert "Codex:codex-ig01-v2-000a" not in almacen.idem


async def test_dos_traspasos_simultaneos_del_responsable_publican_uno(board):
    almacen, slack = board
    barrera = Barrera("A", "aplicar", escribe_encargo)
    almacen.ganchos.append(barrera)
    a = asyncio.create_task(como("codex", coordinacion.registrar_traspaso, **traspaso()), name="A")
    await barrera.esperar()
    gano = await como("codex", coordinacion.registrar_traspaso, **traspaso(entrega_a="Muse", clave_idempotencia="codex-ig01-v2-000b"))
    barrera.soltar()
    with pytest.raises(coordinacion.ErrorBoard) as error:
        await a
    assert error.value.codigo == "no_es_responsable"
    assert almacen.encargo["fuente"]["traspaso_id"] == gano["traspaso_id"]
    assert almacen.encargo["responsable_siguiente"] == "Muse"
    assert len(slack.publicados) == 1


async def test_acuses_simultaneos_publican_una_sola_respuesta(board):
    """IT002B-02 (same_handoff_concurrent_acks): dos acuses con claves distintas del mismo traspaso."""
    almacen, slack = board
    t = await como("codex", coordinacion.registrar_traspaso, **traspaso())
    barrera = Barrera("A", "aplicar", escribe_encargo)
    almacen.ganchos.append(barrera)
    acuse = {"agente": "Claude", "traspaso_id": t["traspaso_id"], "confirmar": True}
    a = asyncio.create_task(como("principal", coordinacion.registrar_acuse, **acuse, clave_idempotencia="claude-acuse-000a"), name="A")
    await barrera.esperar()
    await como("principal", coordinacion.registrar_acuse, **acuse, clave_idempotencia="claude-acuse-000b")
    barrera.soltar()
    with pytest.raises(coordinacion.ErrorBoard) as error:
        await a
    assert error.value.codigo == "ya_confirmado"
    assert len(slack.publicados) == 2  # el traspaso y un solo acuse
    assert almacen.encargo["estado"] == "en_curso" and almacen.encargo["rev"] == 2


async def test_mientras_se_publica_las_demas_escrituras_esperan(board):
    """Con la operación anotada en el encargo, otra escritura recibe operacion_en_curso y no publica."""
    almacen, slack = board
    barrera = Barrera("A", "aplicar", escribe_traspaso)
    almacen.ganchos.append(barrera)
    a = asyncio.create_task(como("codex", coordinacion.registrar_traspaso, **traspaso()), name="A")
    await barrera.esperar()  # A ya publicó y está por guardar
    with pytest.raises(coordinacion.ErrorBoard) as error:
        await como(
            "propietario",
            coordinacion.registrar_traspaso,
            **traspaso(agente="Fabián", entrega_a="Muse", clave_idempotencia="fabian-ig01-v2-000a"),
        )
    assert error.value.codigo == "operacion_en_curso"
    barrera.soltar()
    assert (await a)["duplicado"] is False
    assert len(slack.publicados) == 1
    assert almacen.encargo["responsable_siguiente"] == "Claude"
