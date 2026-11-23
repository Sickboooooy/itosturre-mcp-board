"""Contrato de AlmacenUpstash contra un doble con la misma API que upstash_redis.asyncio.Redis.

Las pruebas «de contrato» corren contra AlmacenUpstash (con RedisDoble) y contra AlmacenMemoria,
el doble que usan las pruebas de coordinación, para que ambos se comporten igual.
"""

from __future__ import annotations

import pytest
from conftest import AlmacenMemoria, RedisDoble

import almacen
from almacen import ENCARGO, apilar_traspaso, borrar, clave_idem, clave_traspaso, poner

pytestmark = pytest.mark.anyio


@pytest.fixture
def doble():
    return RedisDoble()


@pytest.fixture
def alm(doble):
    return almacen.AlmacenUpstash(doble, "preview")


# IT-005: estas cinco pruebas usaban guardar_traspaso_y_encargo y los métodos *_idempotencia,
# que se retiraron. Cada una conserva lo que comprobaba, ahora con leer/aplicar.
async def test_claves_con_prefijo_de_entorno(alm, doble):
    traspaso = {"id": "T-20261122-190512-a3f9", "encargo_id": "IG-01"}
    await alm.aplicar(
        [],
        [poner(clave_traspaso(traspaso["id"]), traspaso), apilar_traspaso(traspaso["id"]), poner(ENCARGO, {"id": "IG-01", "version": "v2"})],
    )
    assert set(doble.datos) == {
        "board:preview:traspaso:T-20261122-190512-a3f9",
        "board:preview:traspasos",
        "board:preview:encargo:vigente",
    }
    assert (await alm.obtener_encargo())["version"] == "v2"
    assert (await alm.obtener_traspaso("T-20261122-190512-a3f9"))["encargo_id"] == "IG-01"


async def test_lista_limitada_y_ordenada(contrato, monkeypatch):
    monkeypatch.setattr(almacen, "MAXIMO_TRASPASOS", 3)
    for i in range(5):
        await contrato.aplicar([], [poner(clave_traspaso(f"T-{i}"), {"id": f"T-{i}"}), apilar_traspaso(f"T-{i}")])
    assert [t["id"] for t in await contrato.listar_traspasos(10)] == ["T-4", "T-3", "T-2"]


async def test_actualizar_traspaso_no_lo_duplica_en_la_lista(contrato):
    await contrato.aplicar([], [poner(clave_traspaso("T-1"), {"id": "T-1"}), apilar_traspaso("T-1")])
    await contrato.aplicar([], [poner(clave_traspaso("T-1"), {"id": "T-1", "acuse": {"de": "Claude"}})])
    listado = await contrato.listar_traspasos(10)
    assert len(listado) == 1 and listado[0]["acuse"]["de"] == "Claude"


async def test_idempotencia_nx_y_ttl(alm, doble):
    k = clave_idem("Codex", "clave-0001")
    assert await alm.aplicar([(k, None)], [poner(k, {"estado": "en_proceso"}, 120)]) is True
    assert await alm.aplicar([(k, None)], [poner(k, {"estado": "en_proceso"}, 120)]) is False
    [(valor, marca)] = await alm.leer(k)
    assert valor == {"estado": "en_proceso"}
    assert doble.ttl["board:preview:idem:Codex:clave-0001"] == 120
    await alm.aplicar([(k, marca)], [poner(k, {"estado": "completado"}, 2592000)])
    assert doble.ttl["board:preview:idem:Codex:clave-0001"] == 2592000
    [(_, marca)] = await alm.leer(k)
    await alm.aplicar([(k, marca)], [borrar(k)])
    assert await alm.leer(k) == [(None, None)]


async def test_fallas_sin_detalles(alm, doble):
    doble.fallar = True
    with pytest.raises(almacen.ErrorAlmacen) as error:
        await alm.obtener_encargo()
    assert error.value.codigo == "almacenamiento_no_disponible"
    assert "secreto" not in str(error.value) and error.value.__cause__ is None
    with pytest.raises(almacen.ErrorAlmacen):
        await alm.aplicar([], [poner(clave_traspaso("T-1"), {"id": "T-1"}), apilar_traspaso("T-1")])


async def test_cerrar(alm, doble):
    await alm.cerrar()
    assert doble.cerrado


def test_sin_credenciales(monkeypatch):
    for variable in ("UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN", "KV_REST_API_URL", "KV_REST_API_TOKEN"):
        monkeypatch.delenv(variable, raising=False)
    with pytest.raises(almacen.ErrorAlmacen) as error:
        almacen.AlmacenUpstash.desde_entorno("preview")
    assert error.value.codigo == "almacenamiento_no_configurado"


@pytest.fixture(params=["upstash", "memoria"])
def contrato(request):
    """Cada prueba de contrato corre contra los dos almacenes."""
    if request.param == "upstash":
        return almacen.AlmacenUpstash(RedisDoble(), "preview")
    return AlmacenMemoria()


async def test_condicion_de_ausencia_es_exclusiva(contrato):
    k = clave_idem("Codex", "clave-0001")
    assert await contrato.aplicar([(k, None)], [poner(k, {"dueno": "a"}, 120)]) is True
    assert await contrato.aplicar([(k, None)], [poner(k, {"dueno": "b"}, 120)]) is False
    [(valor, _)] = await contrato.leer(k)
    assert valor == {"dueno": "a"}


async def test_marca_vieja_no_escribe_nada(contrato):
    """Compare-and-set: si otro escribió después de leer, no se aplica ninguna de las escrituras."""
    t = clave_traspaso("T-20261123-120000-aaaa")
    await contrato.aplicar([], [poner(ENCARGO, {"id": "IG-01", "rev": 1}), poner(t, {"id": "T-20261123-120000-aaaa", "nota": "uno"})])
    [(_, marca_vieja)] = await contrato.leer(ENCARGO)
    assert await contrato.aplicar([(ENCARGO, marca_vieja)], [poner(ENCARGO, {"id": "IG-01", "rev": 2})]) is True
    aplicado = await contrato.aplicar(
        [(ENCARGO, marca_vieja)], [poner(t, {"id": "T-20261123-120000-aaaa", "nota": "dos"}), poner(ENCARGO, {"id": "IG-01", "rev": 3})]
    )
    assert aplicado is False
    [(encargo, _), (traspaso, _)] = await contrato.leer(ENCARGO, t)
    assert encargo == {"id": "IG-01", "rev": 2}
    assert traspaso["nota"] == "uno"


async def test_todas_las_condiciones_deben_cumplirse(contrato):
    k = clave_idem("Codex", "clave-0002")
    await contrato.aplicar([], [poner(ENCARGO, {"rev": 1}), poner(k, {"estado": "en_proceso"})])
    [(_, marca_encargo), (_, marca_idem)] = await contrato.leer(ENCARGO, k)
    await contrato.aplicar([], [poner(k, {"estado": "en_proceso", "dueno": "otro"})])
    assert await contrato.aplicar([(ENCARGO, marca_encargo), (k, marca_idem)], [poner(ENCARGO, {"rev": 2})]) is False
    [(encargo, _)] = await contrato.leer(ENCARGO)
    assert encargo == {"rev": 1}


async def test_la_marca_de_lo_escrito_coincide_con_la_leida(contrato):
    valor = {"responsable_siguiente": "Fabián", "rev": 7, "arriendo": None}
    await contrato.aplicar([], [poner(ENCARGO, valor)])
    [(leido, marca)] = await contrato.leer(ENCARGO)
    assert leido == valor
    assert marca == contrato.marca(valor)
    assert await contrato.aplicar([(ENCARGO, contrato.marca(valor))], [poner(ENCARGO, {**valor, "rev": 8})]) is True


async def test_borrado_condicionado(contrato):
    k = clave_idem("Codex", "clave-0003")
    await contrato.aplicar([], [poner(k, {"dueno": "a"})])
    [(_, marca_a)] = await contrato.leer(k)
    await contrato.aplicar([], [poner(k, {"dueno": "b"})])
    assert await contrato.aplicar([(k, marca_a)], [borrar(k)]) is False
    [(valor, marca_b)] = await contrato.leer(k)
    assert valor == {"dueno": "b"}
    assert await contrato.aplicar([(k, marca_b)], [borrar(k)]) is True
    assert await contrato.leer(k) == [(None, None)]


# --- solo AlmacenUpstash: claves físicas, script y fallas ---
async def test_aplicar_usa_claves_con_prefijo_y_ttl(alm, doble):
    await alm.aplicar(
        [(ENCARGO, None)],
        [
            poner(ENCARGO, {"id": "IG-01"}),
            poner(clave_traspaso("T-20261122-190512-a3f9"), {"id": "T-20261122-190512-a3f9"}),
            apilar_traspaso("T-20261122-190512-a3f9"),
            poner(clave_idem("Codex", "clave-0001"), {"estado": "completado"}, 2592000),
        ],
    )
    assert set(doble.datos) == {
        "board:preview:encargo:vigente",
        "board:preview:traspaso:T-20261122-190512-a3f9",
        "board:preview:traspasos",
        "board:preview:idem:Codex:clave-0001",
    }
    assert doble.ttl["board:preview:idem:Codex:clave-0001"] == 2592000
    assert doble.ttl["board:preview:encargo:vigente"] is None
    # Una sola llamada EVAL: las condiciones van primero en KEYS.
    [(claves, argv)] = doble.evals
    assert claves[0] == "board:preview:encargo:vigente" and argv[:2] == ["1", "!"]


async def test_valor_anterior_que_no_es_json_se_lee_como_texto(alm, doble):
    doble.datos["board:preview:idem:Codex:clave-0001"] = "en_proceso"
    assert await alm.leer(clave_idem("Codex", "clave-0001")) == [("en_proceso", "en_proceso")]


async def test_aplicar_falla_sin_detalles(alm, doble):
    doble.fallar = True
    with pytest.raises(almacen.ErrorAlmacen) as error:
        await alm.aplicar([], [poner(ENCARGO, {"id": "IG-01"})])
    assert error.value.codigo == "almacenamiento_no_disponible"
    assert "secreto" not in str(error.value) and error.value.__cause__ is None
    with pytest.raises(almacen.ErrorAlmacen):
        await alm.leer(ENCARGO)


def test_acepta_variables_kv_y_sin_telemetria(monkeypatch):
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
    monkeypatch.setenv("KV_REST_API_URL", "https://ejemplo.upstash.io")
    monkeypatch.setenv("KV_REST_API_TOKEN", "valor-de-prueba")
    alm = almacen.AlmacenUpstash.desde_entorno("preview")
    assert alm._r._allow_telemetry is False
