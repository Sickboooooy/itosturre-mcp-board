"""IT-005 · Flujo completo por HTTP sobre el adaptador real AlmacenUpstash (con RedisDoble).

Comprueba que las herramientas, el adaptador y el script de compare-and-set (emulado en el
doble) encajan: claves, revisiones y registros de idempotencia tal como quedan en Redis.
"""

from __future__ import annotations

import json

import pytest
from conftest import ENCARGO_INICIAL, TOKENS, RedisDoble, llamar

import almacen as modulo_almacen

pytestmark = pytest.mark.anyio

CLAUDE = TOKENS["MCP_TOKEN"]
CODEX = TOKENS["MCP_TOKEN_CODEX"]
MUSE = TOKENS["MCP_TOKEN_MUSE"]


@pytest.fixture
def redis(board, monkeypatch):
    """Reemplaza el almacén en memoria del fixture `board` por AlmacenUpstash sobre RedisDoble."""
    import coordinacion

    doble = RedisDoble()
    # Encargo cargado a mano como lo hace Fabián hoy: sin rev ni arriendo.
    doble.datos["board:preview:encargo:vigente"] = json.dumps(ENCARGO_INICIAL, ensure_ascii=False)
    monkeypatch.setattr(coordinacion, "crear_almacen", lambda: modulo_almacen.AlmacenUpstash(doble, "preview"))
    return doble


async def test_traspaso_y_acuse_sobre_upstash(cliente_http, board, redis):
    _, slack = board
    args = {
        "agente": "Codex",
        "encargo_id": "IG-01",
        "version": "v2",
        "entrega_a": "Claude",
        "enlace": "https://docs.google.com/document/d/ficticio-v2",
        "nota": "Integré el guion ficticio.",
        "clave_idempotencia": "codex-ig01-v2-0001",
        "confirmar": True,
    }
    _, es_error, t = await llamar(cliente_http, CODEX, "registrar_traspaso", args)
    assert not es_error, t
    _, es_error, repetido = await llamar(cliente_http, CODEX, "registrar_traspaso", args)
    assert not es_error and repetido["duplicado"] is True and repetido["traspaso_id"] == t["traspaso_id"]
    _, es_error, a = await llamar(
        cliente_http,
        CLAUDE,
        "registrar_acuse",
        {"agente": "Claude", "traspaso_id": t["traspaso_id"], "clave_idempotencia": "claude-acuse-0001", "confirmar": True},
    )
    assert not es_error, a

    encargo = json.loads(redis.datos["board:preview:encargo:vigente"])
    assert encargo["rev"] == 2 and encargo["estado"] == "en_curso" and encargo["version"] == "v2"
    assert "operacion_en_curso" not in encargo
    assert redis.datos["board:preview:traspasos"] == [t["traspaso_id"]]
    guardado = json.loads(redis.datos[f"board:preview:traspaso:{t['traspaso_id']}"])
    assert guardado["acuse"]["de"] == "Claude" and guardado["publicacion"]["estado"] == "publicado"
    idem = json.loads(redis.datos["board:preview:idem:Codex:codex-ig01-v2-0001"])
    assert idem["operacion"] == "registrar_traspaso" and idem["estado"] == "completado" and len(idem["huella"]) == 64
    assert redis.ttl["board:preview:idem:Codex:codex-ig01-v2-0001"] == 30 * 24 * 3600
    assert len(slack.publicados) == 2

    _, _, lista = await llamar(cliente_http, MUSE, "listar_traspasos", {"n": 5})
    assert [x["id"] for x in lista["traspasos"]] == [t["traspaso_id"]]
