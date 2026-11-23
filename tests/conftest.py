"""Accesorios de prueba: Redis en memoria y Slack simulado a nivel HTTP.

Las pruebas nunca tocan Slack ni Upstash reales.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "api"))

TOKENS = {
    "MCP_TOKEN": "token-claude-de-prueba-0000000000000000000000",
    "MCP_TOKEN_CODEX": "token-codex-de-prueba-00000000000000000000000",
    "MCP_TOKEN_MUSE": "token-muse-de-prueba-000000000000000000000000",
    "MCP_TOKEN_ANTIGRAVITY": "token-antigravity-de-prueba-00000000000000000",
    "MCP_TOKEN_PRUEBAS": "token-pruebas-de-prueba-000000000000000000000",
    "MCP_TOKEN_OPENAI": "token-openai-de-prueba-0000000000000000000000",
}
TOKEN_SLACK = "xoxb-secreto-de-prueba-NO-DEBE-SALIR"
CANAL_PRUEBAS = "C0CLVFDUN00"

ENCARGO_INICIAL = {
    "id": "IG-01",
    "version": "v1",
    "titulo": "Reel de prueba (ficticio)",
    "estado": "en_curso",
    "responsable_siguiente": "Codex",
    "referencia": "https://docs.google.com/document/d/ficticio",
    "actualizado_en": "2026-11-22T18:00:00Z",
    "fuente": {"tipo": "carga_inicial", "traspaso_id": None, "fecha": "2026-11-22T18:00:00Z"},
    "ultimo_traspaso_permalink": None,
}


def _copia(valor):
    return json.loads(json.dumps(valor)) if valor is not None else None


class AlmacenMemoria:
    """Mismo contrato que AlmacenUpstash, en memoria.

    `leer` y `aplicar` no tienen ningún await entre comprobar y escribir: dentro del bucle de
    eventos cada `aplicar` es atómico, como el script EVAL en Redis. Las pruebas de contrato de
    test_almacen.py corren contra este doble y contra AlmacenUpstash para que no se separen.

    Para las pruebas:
    - `ganchos`: corrutinas `gancho(momento, *datos)` que se llaman después de tomar la foto en
      `leer` ("leer") y antes de comprobar en `aplicar` ("aplicar"). Sirven de barrera para
      intercalar peticiones.
    - `fallar_al_guardar`: falla toda escritura que guarde un traspaso.
    - `fallar_siempre`: falla todo acceso.
    - `perder_respuesta`: predicado sobre las escrituras; si es verdadero, `aplicar` escribe y
      después falla, como cuando Redis ejecutó el script pero la respuesta no llegó.
    """

    def __init__(self):
        self.encargo = None
        self.traspasos: dict[str, dict] = {}
        self.orden: list[str] = []
        self.idem: dict[str, object] = {}
        self.ttl: dict[tuple, int] = {}
        self.fallar_al_guardar = False
        self.fallar_siempre = False
        self.perder_respuesta = None
        self.ganchos: list = []

    # --- contrato nuevo: foto atómica y compare-and-set de varias claves ---
    def _obtener(self, clave: tuple):
        if clave[0] == "encargo":
            return self.encargo
        if clave[0] == "traspaso":
            return self.traspasos.get(clave[1])
        if clave[0] == "idem":
            return self.idem.get(f"{clave[1]}:{clave[2]}")
        raise ValueError(clave)

    def _poner(self, clave: tuple, valor) -> None:
        if clave[0] == "encargo":
            self.encargo = valor
        elif clave[0] == "traspaso":
            if valor is None:
                self.traspasos.pop(clave[1], None)
            else:
                self.traspasos[clave[1]] = valor
        elif clave[0] == "idem":
            if valor is None:
                self.idem.pop(f"{clave[1]}:{clave[2]}", None)
            else:
                self.idem[f"{clave[1]}:{clave[2]}"] = valor
        else:
            raise ValueError(clave)

    def marca(self, valor):
        import almacen

        return None if valor is None else almacen.serializar(valor)

    async def _gancho(self, momento: str, *datos) -> None:
        for gancho in list(self.ganchos):
            await gancho(momento, *datos)

    async def leer(self, *claves):
        import almacen

        if self.fallar_siempre:
            raise almacen.ErrorAlmacen()
        foto = [(_copia(v), self.marca(v)) for v in (self._obtener(c) for c in claves)]
        await self._gancho("leer", claves)
        return foto

    async def aplicar(self, condiciones, escrituras):
        import almacen

        await self._gancho("aplicar", condiciones, escrituras)
        if self.fallar_siempre:
            raise almacen.ErrorAlmacen()
        if self.fallar_al_guardar and any(e[0] == "poner" and e[1][0] == "traspaso" for e in escrituras):
            raise almacen.ErrorAlmacen()
        for clave, marca in condiciones:
            if self.marca(self._obtener(clave)) != marca:
                return False
        for escritura in escrituras:
            if escritura[0] == "poner":
                _, clave, valor, segundos = escritura
                self._poner(clave, _copia(valor))
                if segundos:
                    self.ttl[clave] = segundos
            elif escritura[0] == "borrar":
                self._poner(escritura[1], None)
            elif escritura[0] == "apilar":
                self.orden.insert(0, escritura[1])
                del self.orden[almacen.MAXIMO_TRASPASOS :]
            else:
                raise ValueError(escritura)
        if self.perder_respuesta and self.perder_respuesta(escrituras):
            raise almacen.ErrorAlmacen()
        return True

    # --- contrato anterior (lo retira IT-005 cuando coordinacion.py deje de usarlo) ---

    async def obtener_encargo(self):
        return json.loads(json.dumps(self.encargo)) if self.encargo else None

    async def obtener_traspaso(self, traspaso_id):
        t = self.traspasos.get(traspaso_id)
        return json.loads(json.dumps(t)) if t else None

    async def guardar_traspaso_y_encargo(self, traspaso, encargo, nuevo):
        import almacen

        if self.fallar_al_guardar:
            raise almacen.ErrorAlmacen()
        self.traspasos[traspaso["id"]] = json.loads(json.dumps(traspaso))
        if nuevo:
            self.orden.insert(0, traspaso["id"])
        if encargo is not None:
            self.encargo = json.loads(json.dumps(encargo))

    async def listar_traspasos(self, limite):
        return [json.loads(json.dumps(self.traspasos[i])) for i in self.orden[:limite]]

    async def leer_idempotencia(self, agente, clave):
        return self.idem.get(f"{agente}:{clave}")

    async def reservar_idempotencia(self, agente, clave, segundos):
        k = f"{agente}:{clave}"
        if k in self.idem:
            return False
        self.idem[k] = "en_proceso"
        return True

    async def guardar_idempotencia(self, agente, clave, valor, segundos):
        self.idem[f"{agente}:{clave}"] = valor

    async def liberar_idempotencia(self, agente, clave):
        self.idem.pop(f"{agente}:{clave}", None)

    async def cerrar(self):
        pass


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
    """Doble con la misma API que upstash_redis.asyncio.Redis para lo que usa AlmacenUpstash.

    EVAL solo acepta almacen.SCRIPT_CAS y reproduce su semántica en Python, paso por paso.
    No hay intérprete Lua ni Redis en el entorno de pruebas: el script real no se ejecuta aquí.
    """

    def __init__(self):
        self.datos = {}
        self.ttl = {}
        self.fallar = False
        self.cerrado = False
        self.evals: list[tuple[list[str], list[str]]] = []

    def _quizas_fallar(self):
        if self.fallar:
            raise RuntimeError("error de red con token upstash-secreto")

    async def get(self, key):
        self._quizas_fallar()
        return self.datos.get(key)

    async def set(self, key, value, nx=None, xx=None, get=None, ex=None, px=None, exat=None, pxat=None, keepttl=None):
        self._quizas_fallar()
        if nx and key in self.datos:
            return None
        self.datos[key] = str(value)
        self.ttl[key] = ex
        return "OK"

    async def mget(self, *keys):
        self._quizas_fallar()
        return [self.datos.get(k) if isinstance(self.datos.get(k), str) else None for k in keys]

    async def lrange(self, key, start, stop):
        self._quizas_fallar()
        return self.datos.get(key, [])[start : stop + 1]

    async def delete(self, *keys):
        self._quizas_fallar()
        return sum(1 for k in keys if self.datos.pop(k, None) is not None)

    def multi(self):
        return PipelineDoble(self)

    async def eval(self, script, keys=None, args=None):
        import almacen

        self._quizas_fallar()
        assert script == almacen.SCRIPT_CAS, "el doble solo sabe ejecutar SCRIPT_CAS"
        claves = list(keys or [])
        argv = [str(a) for a in (args or [])]  # el SDK envía todo como texto
        self.evals.append((claves, argv))
        # 1) condiciones: KEYS[1..n] deben conservar la marca ("=" + texto) o no existir ("!")
        n = int(argv[0])
        for i in range(n):
            esperado, actual = argv[1 + i], self.datos.get(claves[i])
            if esperado == "!":
                if actual is not None:
                    return 0
            elif actual != esperado[1:]:
                return 0
        # 2) escrituras en grupos de cuatro: operación, índice de KEYS (desde 1), valor, número
        grupos = [argv[j : j + 4] for j in range(1 + n, len(argv), 4)]
        if any(len(g) != 4 or g[0] not in ("SET", "DEL", "LPUSH") for g in grupos):
            raise RuntimeError("ERR operacion_desconocida")
        for operacion, indice, valor, numero in grupos:
            clave = claves[int(indice) - 1]
            if operacion == "SET":
                self.datos[clave] = valor
                self.ttl[clave] = int(numero) if int(numero) > 0 else None
            elif operacion == "DEL":
                self.datos.pop(clave, None)
                self.ttl.pop(clave, None)
            else:
                lista = self.datos.setdefault(clave, [])
                lista.insert(0, valor)
                del lista[int(numero) :]
        return 1

    async def close(self):
        self.cerrado = True


class SlackSimulado:
    """Responde como la API de Slack y registra lo que se le envía."""

    def __init__(self):
        self.peticiones: list[httpx.Request] = []
        self.modo = "ok"  # ok | error | excepcion
        self.codigo_error = "not_in_channel"
        self._contador = 0

    def manejador(self, peticion: httpx.Request) -> httpx.Response:
        self.peticiones.append(peticion)
        if self.modo == "excepcion":
            raise httpx.ConnectError(f"fallo de red con cabecera Bearer {TOKEN_SLACK}")
        if self.modo == "error":
            return httpx.Response(200, json={"ok": False, "error": self.codigo_error})
        if peticion.url.path.endswith("chat.postMessage"):
            self._contador += 1
            return httpx.Response(200, json={"ok": True, "ts": f"1795370000.{self._contador:06d}"})
        if peticion.url.path.endswith("chat.getPermalink"):
            ts = peticion.url.params["message_ts"].replace(".", "")
            return httpx.Response(200, json={"ok": True, "permalink": f"https://itosturre.slack.com/archives/{CANAL_PRUEBAS}/p{ts}"})
        return httpx.Response(404)

    @property
    def mensajes(self) -> list[dict]:
        return [json.loads(p.content) for p in self.peticiones if p.url.path.endswith("chat.postMessage")]


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def entorno(monkeypatch):
    for variable, valor in TOKENS.items():
        monkeypatch.setenv(variable, valor)
    monkeypatch.setenv("VERCEL_ENV", "preview")
    monkeypatch.setenv("SLACK_BOT_TOKEN", TOKEN_SLACK)
    monkeypatch.setenv("SLACK_CANAL_BOARD", CANAL_PRUEBAS)
    monkeypatch.setenv("SLACK_CANALES_PERMITIDOS", CANAL_PRUEBAS)
    monkeypatch.delenv("ENLACES_DOMINIOS_PERMITIDOS", raising=False)


@pytest.fixture
def board(entorno, monkeypatch):
    """Devuelve (almacen, slack) ya conectados a las herramientas."""
    import coordinacion
    import publicador_slack

    almacen = AlmacenMemoria()
    almacen.encargo = json.loads(json.dumps(ENCARGO_INICIAL))
    slack = SlackSimulado()
    monkeypatch.setattr(coordinacion, "crear_almacen", lambda: almacen)

    def _publicador():
        base = publicador_slack.PublicadorSlack.desde_entorno()
        base._transporte = httpx.MockTransport(slack.manejador)
        return base

    monkeypatch.setattr(coordinacion, "crear_publicador", _publicador)
    return almacen, slack


@pytest.fixture
async def cliente_http():
    import index

    transporte = httpx.ASGITransport(app=index.app)
    async with httpx.AsyncClient(transport=transporte, base_url="https://board.test") as cliente:
        yield cliente


async def llamar(cliente, token: str | None, herramienta: str, argumentos: dict | None = None):
    """Llama una herramienta por JSON-RPC. Devuelve (estado_http, es_error, contenido)."""
    cabeceras = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
    if token is not None:
        cabeceras["authorization"] = f"Bearer {token}"
    cuerpo = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": herramienta, "arguments": argumentos or {}}}
    respuesta = await cliente.post("/mcp", headers=cabeceras, json=cuerpo)
    if respuesta.status_code != 200:
        return respuesta.status_code, True, respuesta.text
    resultado = respuesta.json()["result"]
    es_error = bool(resultado.get("isError"))
    if es_error:
        # El SDK antepone "Error executing tool <nombre>: " al JSON de ErrorBoard.
        texto = resultado["content"][0]["text"]
        inicio = texto.find("{")
        try:
            return 200, True, json.loads(texto[inicio:]) if inicio >= 0 else texto
        except ValueError:
            return 200, True, texto
    contenido = resultado.get("structuredContent")
    if contenido is None:
        contenido = json.loads(resultado["content"][0]["text"])
    return 200, False, contenido
