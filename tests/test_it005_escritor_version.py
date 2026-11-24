"""IT-005 · Un escritor (riesgo 5), control de versión (riesgo 6) y acuse solo del traspaso vigente (riesgo 8).

El encargo inicial de conftest está en v1 con responsable Codex. Slack y Redis están simulados.
"""

from __future__ import annotations

import pytest
from conftest import TOKENS, llamar

pytestmark = pytest.mark.anyio

CLAUDE = TOKENS["MCP_TOKEN"]
CODEX = TOKENS["MCP_TOKEN_CODEX"]
MUSE = TOKENS["MCP_TOKEN_MUSE"]
PROPIETARIO = TOKENS["MCP_TOKEN_PROPIETARIO"]


def traspaso(**cambios) -> dict:
    base = {
        "agente": "Codex",
        "encargo_id": "IG-01",
        "version": "v2",
        "entrega_a": "Claude",
        "enlace": "https://docs.google.com/document/d/ficticio-v2",
        "nota": "Integré el guion ficticio.",
        "clave_idempotencia": "codex-ig01-v2-0001",
        "confirmar": True,
    }
    base.update(cambios)
    return base


def acuse(agente: str, traspaso_id: str, clave: str, **cambios) -> dict:
    return {"agente": agente, "traspaso_id": traspaso_id, "clave_idempotencia": clave, "confirmar": True, **cambios}


# --- riesgo 5: un escritor ---
async def test_quien_no_es_responsable_no_puede_traspasar(cliente_http, board):
    almacen, slack = board
    _, es_error, datos = await llamar(
        cliente_http, MUSE, "registrar_traspaso", traspaso(agente="Muse", clave_idempotencia="muse-ig01-v2-0001")
    )
    assert es_error and datos["error"] == "no_es_responsable"
    assert "Codex" in datos["mensaje"]
    assert slack.peticiones == []
    assert almacen.encargo["version"] == "v1" and almacen.encargo["responsable_siguiente"] == "Codex"


async def test_quien_ya_entrego_deja_de_ser_escritor(cliente_http, board):
    almacen, slack = board
    _, es_error, _ = await llamar(cliente_http, CODEX, "registrar_traspaso", traspaso())
    assert not es_error
    _, es_error, datos = await llamar(
        cliente_http, CODEX, "registrar_traspaso", traspaso(version="v3", entrega_a="Muse", clave_idempotencia="codex-ig01-v3-0001")
    )
    assert es_error and datos["error"] == "no_es_responsable"
    assert len(slack.publicados) == 1
    assert almacen.encargo["responsable_siguiente"] == "Claude"


async def test_el_propietario_puede_traspasar_aunque_no_sea_responsable(cliente_http, board):
    almacen, slack = board
    _, es_error, datos = await llamar(
        cliente_http,
        PROPIETARIO,
        "registrar_traspaso",
        traspaso(agente="Fabián", entrega_a="Muse", clave_idempotencia="fabian-ig01-v2-0001"),
    )
    assert not es_error, datos
    assert almacen.encargo["responsable_siguiente"] == "Muse"
    assert almacen.traspasos[datos["traspaso_id"]]["de"] == "Fabián"
    assert len(slack.publicados) == 1


async def test_sin_su_token_nadie_firma_como_el_propietario(cliente_http, board):
    _, slack = board
    _, es_error, datos = await llamar(cliente_http, CODEX, "registrar_traspaso", traspaso(agente="Fabián", entrega_a="Muse"))
    assert es_error and datos["error"] == "agente_no_coincide"
    assert slack.peticiones == []


# --- riesgo 6: control de versión estricto ---
async def test_repetir_la_version_se_rechaza_sin_justificacion(cliente_http, board):
    almacen, slack = board
    await llamar(cliente_http, CODEX, "registrar_traspaso", traspaso())
    _, es_error, datos = await llamar(
        cliente_http, CLAUDE, "registrar_traspaso", traspaso(agente="Claude", entrega_a="Muse", clave_idempotencia="claude-ig01-v2-0001")
    )
    assert es_error and datos["error"] == "version_repetida"
    assert "v3" in datos["mensaje"]
    assert len(slack.publicados) == 1
    assert almacen.encargo["responsable_siguiente"] == "Claude"


async def test_saltar_versiones_se_rechaza_sin_justificacion(cliente_http, board):
    almacen, slack = board
    _, es_error, datos = await llamar(cliente_http, CODEX, "registrar_traspaso", traspaso(version="v3"))
    assert es_error and datos["error"] == "version_salto"
    assert slack.peticiones == []
    assert almacen.encargo["version"] == "v1"


async def test_repetir_o_saltar_con_justificacion_queda_registrado(cliente_http, board):
    almacen, slack = board
    motivo = "Se descartaron v2 y v3 en la revisión con Fabián <!channel>."
    _, es_error, t1 = await llamar(cliente_http, CODEX, "registrar_traspaso", traspaso(version="v4", justificacion_version=motivo))
    assert not es_error, t1
    assert almacen.traspasos[t1["traspaso_id"]]["justificacion_version"] == motivo
    assert "Se descartaron v2 y v3 en la revisión con Fabián &lt;!channel&gt;." in slack.publicados[0]["text"]
    _, es_error, t2 = await llamar(
        cliente_http,
        CLAUDE,
        "registrar_traspaso",
        traspaso(
            agente="Claude",
            version="v4",
            entrega_a="Muse",
            clave_idempotencia="claude-ig01-v4-0001",
            justificacion_version="Paso a Muse la misma versión para revisión visual.",
        ),
    )
    assert not es_error, t2
    assert almacen.encargo["version"] == "v4" and almacen.encargo["responsable_siguiente"] == "Muse"


async def test_retroceder_nunca_se_acepta(cliente_http, board):
    almacen, _ = board
    almacen.encargo["version"] = "v3"
    _, es_error, datos = await llamar(
        cliente_http, CODEX, "registrar_traspaso", traspaso(version="v2", justificacion_version="Quiero volver a v2.")
    )
    assert es_error and datos["error"] == "version_anterior"


async def test_cada_escritura_sube_la_revision(cliente_http, board):
    _, _, encargo = await llamar(cliente_http, MUSE, "obtener_encargo_vigente")
    assert encargo["rev"] == 0  # encargo cargado a mano, sin rev
    _, _, t = await llamar(cliente_http, CODEX, "registrar_traspaso", traspaso())
    assert t["encargo"]["rev"] == 1
    await llamar(cliente_http, CLAUDE, "registrar_acuse", acuse("Claude", t["traspaso_id"], "claude-acuse-0001"))
    _, _, encargo = await llamar(cliente_http, MUSE, "obtener_encargo_vigente")
    assert encargo["rev"] == 2 and encargo["estado"] == "en_curso"


async def test_rev_esperada_desactualizada_se_rechaza(cliente_http, board):
    almacen, slack = board
    _, es_error, t = await llamar(cliente_http, CODEX, "registrar_traspaso", traspaso(rev_esperada=0))
    assert not es_error, t
    _, es_error, datos = await llamar(
        cliente_http, CLAUDE, "registrar_acuse", acuse("Claude", t["traspaso_id"], "claude-acuse-0001", rev_esperada=0)
    )
    assert es_error and datos["error"] == "conflicto_revision"
    assert almacen.encargo["estado"] == "pendiente_acuse"
    _, es_error, datos = await llamar(
        cliente_http, CLAUDE, "registrar_acuse", acuse("Claude", t["traspaso_id"], "claude-acuse-0002", rev_esperada=1)
    )
    assert not es_error, datos
    assert len(slack.publicados) == 2


# --- riesgo 8: el acuse solo vale para el traspaso vigente ---
async def test_acuse_de_un_traspaso_anterior_no_cambia_el_vigente(cliente_http, board):
    """IT002B-06: Codex entrega a Claude (T1), Claude entrega a Muse (T2) y después acusa T1."""
    almacen, slack = board
    _, _, t1 = await llamar(cliente_http, CODEX, "registrar_traspaso", traspaso())
    _, _, t2 = await llamar(
        cliente_http, CLAUDE, "registrar_traspaso", traspaso(agente="Claude", version="v3", entrega_a="Muse", clave_idempotencia="claude-ig01-v3-0001")
    )
    _, es_error, datos = await llamar(cliente_http, CLAUDE, "registrar_acuse", acuse("Claude", t1["traspaso_id"], "claude-acuse-t1-0001"))
    assert es_error and datos["error"] == "traspaso_no_vigente"
    assert almacen.encargo["estado"] == "pendiente_acuse" and almacen.encargo["responsable_siguiente"] == "Muse"
    assert almacen.traspasos[t1["traspaso_id"]]["acuse"] is None
    assert len(slack.publicados) == 2
    _, es_error, datos = await llamar(cliente_http, MUSE, "registrar_acuse", acuse("Muse", t2["traspaso_id"], "muse-acuse-t2-0001"))
    assert not es_error, datos
    assert almacen.encargo["estado"] == "en_curso"


async def test_control_acuse_antiguo_con_la_misma_version_y_justificacion_valida_se_rechaza(cliente_http, board):
    """CONTROL pedido por Codex (pasa antes y después de las correcciones de IT005-R01 a R03): IT002B-06
    tal cual, con la misma versión. Codex entrega v2 a Claude (T1); Claude entrega a Muse la misma v2
    con una justificación válida (T2) y después acusa T1. El acuse se rechaza por el identificador del
    traspaso vigente, no por la versión."""
    almacen, slack = board
    _, es_error, t1 = await llamar(cliente_http, CODEX, "registrar_traspaso", traspaso())
    assert not es_error, t1
    _, es_error, t2 = await llamar(
        cliente_http,
        CLAUDE,
        "registrar_traspaso",
        traspaso(
            agente="Claude",
            entrega_a="Muse",
            clave_idempotencia="claude-ig01-v2-0001",
            justificacion_version="Paso a Muse la misma versión para la revisión visual.",
        ),
    )
    assert not es_error, t2
    assert almacen.traspasos[t1["traspaso_id"]]["version"] == almacen.traspasos[t2["traspaso_id"]]["version"] == "v2"
    assert almacen.encargo["traspaso_vigente"] == t2["traspaso_id"]
    _, es_error, datos = await llamar(cliente_http, CLAUDE, "registrar_acuse", acuse("Claude", t1["traspaso_id"], "claude-acuse-t1-0001"))
    assert es_error and datos["error"] == "traspaso_no_vigente", datos
    assert almacen.encargo["estado"] == "pendiente_acuse" and almacen.encargo["responsable_siguiente"] == "Muse"
    assert almacen.traspasos[t1["traspaso_id"]]["acuse"] is None
    assert len(slack.publicados) == 2
    _, es_error, datos = await llamar(cliente_http, MUSE, "registrar_acuse", acuse("Muse", t2["traspaso_id"], "muse-acuse-t2-0001"))
    assert not es_error, datos
    assert almacen.encargo["estado"] == "en_curso"
