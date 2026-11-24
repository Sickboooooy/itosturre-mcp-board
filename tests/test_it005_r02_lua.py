"""IT-005 · IT005-R02: SCRIPT_CAS escribe todo o nada, y un índice de listado dañado no bloquea el encargo.

Lo esencial de estas pruebas corre el texto real de `almacen.SCRIPT_CAS` con Lua 5.1 (lupa) a
través de fakeredis (`RedisLua`, en conftest), no la emulación de RedisDoble. Las mismas
comprobaciones corren también contra RedisDoble para que su emulación no se aparte del script.
Si lupa no está instalado (requirements-dev.txt), los casos con Lua se omiten.
"""

from __future__ import annotations

import json

import pytest
from conftest import RedisDoble, RedisLua, como

import almacen
import coordinacion
from almacen import ENCARGO, apilar_traspaso, clave_idem, clave_traspaso, poner

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
LISTA = "board:preview:traspasos"
DANADO = "texto que no es una lista"


@pytest.fixture(params=["lua", "doble"])
def redis(request):
    """El script real con Lua (lo que prueba IT005-R02) y su emulación (fidelidad del doble)."""
    if request.param == "lua":
        pytest.importorskip("lupa")
        return RedisLua()
    return RedisDoble()


def _sembrar(redis, clave: str, valor: str) -> None:
    if isinstance(redis, RedisLua):
        redis.r.set(clave, valor)
    else:
        redis.datos[clave] = valor


def _foto(redis) -> dict:
    return json.loads(json.dumps(redis.datos))


async def _eval(redis, claves: list[str], argv: list[str]):
    """Resultado del script, o None si terminó con error."""
    try:
        return await redis.eval(almacen.SCRIPT_CAS, keys=claves, args=argv)
    except Exception:
        return None


# Cada caso: claves (KEYS), valores previos y ARGV. Siempre hay una escritura válida antes de la que
# falla, para que una escritura parcial se note.
CASOS = {
    "lista_de_tipo_incorrecto": (["a", "lista"], {"lista": DANADO}, ["0", "SET", "1", "nuevo", "0", "LPUSH", "2", "T-1", "200"]),
    "segundos_no_numericos": (["a", "b"], {}, ["0", "SET", "1", "nuevo", "0", "SET", "2", "otro", "abc"]),
    "segundos_decimales": (["a", "b"], {}, ["0", "SET", "1", "nuevo", "0", "SET", "2", "otro", "1.5"]),
    "largo_de_lista_no_numerico": (["a", "lista"], {}, ["0", "SET", "1", "nuevo", "0", "LPUSH", "2", "T-1", "x"]),
    "indice_fuera_de_rango": (["a", "b"], {}, ["0", "SET", "1", "nuevo", "0", "SET", "3", "otro", "0"]),
    "indice_cero": (["a", "b"], {"b": "previo"}, ["0", "SET", "1", "nuevo", "0", "DEL", "0", "", "0"]),
    "indice_no_numerico": (["a", "b"], {"b": "previo"}, ["0", "SET", "1", "nuevo", "0", "DEL", "x", "", "0"]),
}


@pytest.mark.parametrize("caso", list(CASOS))
async def test_un_error_previsible_no_deja_escrituras_parciales(redis, caso):
    claves, previos, argv = CASOS[caso]
    for clave, valor in previos.items():
        _sembrar(redis, clave, valor)
    antes = _foto(redis)
    resultado = await _eval(redis, claves, argv)
    assert _foto(redis) == antes, "el script dejó escrituras parciales"
    assert resultado != 1


async def test_aplicar_con_la_lista_de_tipo_incorrecto_no_escribe_nada_y_lo_avisa(redis):
    """El caso de Codex y Hermes: completar un traspaso con la lista de traspasos dañada."""
    alm = almacen.AlmacenUpstash(redis, "preview")
    _sembrar(redis, "board:preview:encargo:vigente", almacen.serializar({"id": "IG-01", "rev": 1}))
    _sembrar(redis, LISTA, DANADO)
    antes = _foto(redis)
    [(_, marca)] = await alm.leer(ENCARGO)
    try:
        await alm.aplicar(
            [(ENCARGO, marca)],
            [
                poner(clave_traspaso("T-20261123-120000-aaaa"), {"id": "T-20261123-120000-aaaa"}),
                apilar_traspaso("T-20261123-120000-aaaa"),
                poner(ENCARGO, {"id": "IG-01", "rev": 2}),
                poner(clave_idem("Codex", "clave-0001"), {"estado": "completado"}, 2592000),
            ],
        )
        error = None
    except almacen.ErrorAlmacen as e:
        error = e
    assert _foto(redis) == antes, "el script dejó escrituras parciales"
    assert error is not None and error.codigo == "indice_danado"


async def test_control_escrituras_validas_se_aplican_todas_con_lua():
    """CONTROL (pasa antes y después): con argumentos válidos el script real escribe todo."""
    pytest.importorskip("lupa")
    redis = RedisLua()
    alm = almacen.AlmacenUpstash(redis, "preview")
    assert await alm.aplicar(
        [(ENCARGO, None)],
        [
            poner(ENCARGO, {"id": "IG-01"}),
            poner(clave_traspaso("T-1"), {"id": "T-1"}),
            apilar_traspaso("T-1"),
            poner(clave_idem("Codex", "clave-0001"), {"estado": "completado"}, 2592000),
        ],
    )
    assert redis.datos == {
        "board:preview:encargo:vigente": '{"id": "IG-01"}',
        "board:preview:idem:Codex:clave-0001": '{"estado": "completado"}',
        "board:preview:traspaso:T-1": '{"id": "T-1"}',
        LISTA: ["T-1"],
    }
    assert 0 < redis.r.ttl("board:preview:idem:Codex:clave-0001") <= 2592000
    assert redis.r.ttl("board:preview:encargo:vigente") == -1


async def test_indice_de_listado_danado_no_bloquea_el_encargo(board_lua):
    """Con la lista de traspasos dañada, el traspaso se completa igual (sin listarlo) y el
    siguiente escritor no queda bloqueado. Antes, el cierre fallaba siempre con registro_incompleto."""
    redis, slack = board_lua
    redis.r.set(LISTA, DANADO)
    resultado = await como("codex", coordinacion.registrar_traspaso, **TRASPASO)
    assert resultado["duplicado"] is False
    encargo = json.loads(redis.r.get("board:preview:encargo:vigente"))
    assert encargo["version"] == "v2" and encargo["traspaso_vigente"] == resultado["traspaso_id"]
    assert "operacion_en_curso" not in encargo
    idem = json.loads(redis.r.get("board:preview:idem:Codex:codex-ig01-v2-0001"))
    assert idem["estado"] == "completado"
    traspaso = json.loads(redis.r.get(f"board:preview:traspaso:{resultado['traspaso_id']}"))
    assert traspaso["slack_ts"] == slack.publicados[0]["ts"]
    assert redis.r.get(LISTA) == DANADO  # el índice no se tocó: lo repara Fabián
    assert len(slack.mensajes) == 1
    # El reintento no publica de nuevo y el siguiente escritor avanza.
    otra = await como("codex", coordinacion.registrar_traspaso, **TRASPASO)
    assert otra["duplicado"] is True and len(slack.mensajes) == 1
    await como(
        "principal",
        coordinacion.registrar_acuse,
        agente="Claude",
        traspaso_id=resultado["traspaso_id"],
        clave_idempotencia="claude-acuse-0001",
        confirmar=True,
    )
    assert json.loads(redis.r.get("board:preview:encargo:vigente"))["estado"] == "en_curso"
