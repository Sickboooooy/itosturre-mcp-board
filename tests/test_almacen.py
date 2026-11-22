"""Contrato de AlmacenUpstash contra un doble con la misma API que upstash_redis.asyncio.Redis."""

from __future__ import annotations

import pytest

import almacen

pytestmark = pytest.mark.anyio


class PipelineDoble:
    """Como AsyncPipeline: los comandos se encolan sin await y exec() es asíncrono."""

    def __init__(self, redis):
        self._r = redis
        self._cola = []

    def set(self, key, value, nx=None, xx=None, get=None, ex=None, px=None, exat=None, pxat=None, keepttl=None):
        self._cola.append(("set", key, value))
        return self

    def lpush(self, key, *elements):
        self._cola.append(("lpush", key, elements))
        return self

    def ltrim(self, key, start, stop):
        self._cola.append(("ltrim", key, start, stop))
        return self

    async def exec(self):
        if self._r.fallar:
            raise RuntimeError("error de red con token upstash-secreto")
        for orden in self._cola:
            if orden[0] == "set":
                self._r.datos[orden[1]] = str(orden[2])
            elif orden[0] == "lpush":
                lista = self._r.datos.setdefault(orden[1], [])
                for elemento in orden[2]:
                    lista.insert(0, elemento)
            elif orden[0] == "ltrim":
                self._r.datos[orden[1]] = self._r.datos.get(orden[1], [])[orden[2] : orden[3] + 1]
        return ["OK"] * len(self._cola)


class RedisDoble:
    def __init__(self):
        self.datos = {}
        self.ttl = {}
        self.fallar = False
        self.cerrado = False

    async def get(self, key):
        if self.fallar:
            raise RuntimeError("error de red con token upstash-secreto")
        return self.datos.get(key)

    async def set(self, key, value, nx=None, xx=None, get=None, ex=None, px=None, exat=None, pxat=None, keepttl=None):
        if nx and key in self.datos:
            return None
        self.datos[key] = str(value)
        self.ttl[key] = ex
        return "OK"

    async def mget(self, *keys):
        return [self.datos.get(k) for k in keys]

    async def lrange(self, key, start, stop):
        return self.datos.get(key, [])[start : stop + 1]

    async def delete(self, *keys):
        return sum(1 for k in keys if self.datos.pop(k, None) is not None)

    def multi(self):
        return PipelineDoble(self)

    async def close(self):
        self.cerrado = True


@pytest.fixture
def doble():
    return RedisDoble()


@pytest.fixture
def alm(doble):
    return almacen.AlmacenUpstash(doble, "preview")


async def test_claves_con_prefijo_de_entorno(alm, doble):
    traspaso = {"id": "T-20261122-190512-a3f9", "encargo_id": "IG-01"}
    await alm.guardar_traspaso_y_encargo(traspaso, {"id": "IG-01", "version": "v2"}, nuevo=True)
    assert set(doble.datos) == {
        "board:preview:traspaso:T-20261122-190512-a3f9",
        "board:preview:traspasos",
        "board:preview:encargo:vigente",
    }
    assert (await alm.obtener_encargo())["version"] == "v2"
    assert (await alm.obtener_traspaso("T-20261122-190512-a3f9"))["encargo_id"] == "IG-01"


async def test_lista_limitada_y_ordenada(alm, monkeypatch):
    monkeypatch.setattr(almacen, "MAXIMO_TRASPASOS", 3)
    for i in range(5):
        await alm.guardar_traspaso_y_encargo({"id": f"T-{i}"}, None, nuevo=True)
    assert [t["id"] for t in await alm.listar_traspasos(10)] == ["T-4", "T-3", "T-2"]


async def test_actualizar_traspaso_no_lo_duplica_en_la_lista(alm):
    await alm.guardar_traspaso_y_encargo({"id": "T-1"}, None, nuevo=True)
    await alm.guardar_traspaso_y_encargo({"id": "T-1", "acuse": {"de": "Claude"}}, None, nuevo=False)
    listado = await alm.listar_traspasos(10)
    assert len(listado) == 1 and listado[0]["acuse"]["de"] == "Claude"


async def test_idempotencia_nx_y_ttl(alm, doble):
    assert await alm.reservar_idempotencia("Codex", "clave-0001", 120) is True
    assert await alm.reservar_idempotencia("Codex", "clave-0001", 120) is False
    assert await alm.leer_idempotencia("Codex", "clave-0001") == almacen.EN_PROCESO
    await alm.guardar_idempotencia("Codex", "clave-0001", '{"ok": 1}', 2592000)
    assert doble.ttl["board:preview:idem:Codex:clave-0001"] == 2592000
    await alm.liberar_idempotencia("Codex", "clave-0001")
    assert await alm.leer_idempotencia("Codex", "clave-0001") is None


async def test_fallas_sin_detalles(alm, doble):
    doble.fallar = True
    with pytest.raises(almacen.ErrorAlmacen) as error:
        await alm.obtener_encargo()
    assert error.value.codigo == "almacenamiento_no_disponible"
    assert "secreto" not in str(error.value) and error.value.__cause__ is None
    with pytest.raises(almacen.ErrorAlmacen):
        await alm.guardar_traspaso_y_encargo({"id": "T-1"}, None, nuevo=True)


async def test_cerrar(alm, doble):
    await alm.cerrar()
    assert doble.cerrado


def test_sin_credenciales(monkeypatch):
    for variable in ("UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN", "KV_REST_API_URL", "KV_REST_API_TOKEN"):
        monkeypatch.delenv(variable, raising=False)
    with pytest.raises(almacen.ErrorAlmacen) as error:
        almacen.AlmacenUpstash.desde_entorno("preview")
    assert error.value.codigo == "almacenamiento_no_configurado"


def test_acepta_variables_kv_y_sin_telemetria(monkeypatch):
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
    monkeypatch.setenv("KV_REST_API_URL", "https://ejemplo.upstash.io")
    monkeypatch.setenv("KV_REST_API_TOKEN", "valor-de-prueba")
    alm = almacen.AlmacenUpstash.desde_entorno("preview")
    assert alm._r._allow_telemetry is False
