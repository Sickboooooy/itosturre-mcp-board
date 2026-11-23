"""Accesorios de prueba: Redis en memoria y Slack simulado a nivel HTTP.

Las pruebas nunca tocan Slack ni Upstash reales.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
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
    "MCP_TOKEN_PROPIETARIO": "token-propietario-de-prueba-00000000000000000",
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


    # --- lecturas simples ---
    async def obtener_encargo(self):
        return _copia(self.encargo) if self.encargo else None

    async def obtener_traspaso(self, traspaso_id):
        t = self.traspasos.get(traspaso_id)
        return _copia(t) if t else None

    async def listar_traspasos(self, limite):
        return [_copia(self.traspasos[i]) for i in self.orden[:limite]]

    async def cerrar(self):
        pass


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
    """Responde como la API de Slack y registra lo que se le envía.

    - `mensajes`: cuerpos de todas las peticiones chat.postMessage que salieron.
    - `publicados`: lo que Slack sí guardó y muestra en el canal (con ts, metadatos e hilo).
    Fallos simulables (cada contador se consume en una publicación):
    - `perder_respuesta`: Slack guarda el mensaje pero la respuesta no llega (ReadTimeout).
    - `cortar_antes_de_guardar`: la petición sale pero Slack no la guarda (ReadTimeout).
    - `cancelar_publicacion`: el proceso muere antes de enviar (CancelledError).
    - `historial_disponible = False`: conversations.* responde missing_scope.
    """

    def __init__(self):
        self.peticiones: list[httpx.Request] = []
        self.modo = "ok"  # ok | error | excepcion
        self.codigo_error = "not_in_channel"
        self._contador = 0
        self.publicados: list[dict] = []
        self.perder_respuesta = 0
        self.cortar_antes_de_guardar = 0
        self.cancelar_publicacion = 0
        self.historial_disponible = True

    def manejador(self, peticion: httpx.Request) -> httpx.Response:
        ruta = peticion.url.path
        if ruta.endswith("chat.postMessage") and self.cancelar_publicacion:
            self.cancelar_publicacion -= 1
            raise asyncio.CancelledError()
        self.peticiones.append(peticion)
        if self.modo == "excepcion":
            raise httpx.ConnectError(f"fallo de red con cabecera Bearer {TOKEN_SLACK}")
        if self.modo == "error":
            return httpx.Response(200, json={"ok": False, "error": self.codigo_error})
        if ruta.endswith("chat.postMessage"):
            if self.cortar_antes_de_guardar:
                self.cortar_antes_de_guardar -= 1
                raise httpx.ReadTimeout("sin respuesta", request=peticion)
            self._contador += 1
            ts = f"1795370000.{self._contador:06d}"
            cuerpo = json.loads(peticion.content)
            self.publicados.append(
                {"ts": ts, "text": cuerpo["text"], "metadata": cuerpo.get("metadata"), "thread_ts": cuerpo.get("thread_ts")}
            )
            if self.perder_respuesta:
                self.perder_respuesta -= 1
                raise httpx.ReadTimeout("respuesta perdida", request=peticion)
            return httpx.Response(200, json={"ok": True, "ts": ts})
        if ruta.endswith("chat.getPermalink"):
            ts = peticion.url.params["message_ts"].replace(".", "")
            return httpx.Response(200, json={"ok": True, "permalink": f"https://itosturre.slack.com/archives/{CANAL_PRUEBAS}/p{ts}"})
        if ruta.endswith("conversations.history") or ruta.endswith("conversations.replies"):
            if not self.historial_disponible:
                return httpx.Response(200, json={"ok": False, "error": "missing_scope"})
            if peticion.url.params.get("channel") != CANAL_PRUEBAS:
                return httpx.Response(200, json={"ok": False, "error": "channel_not_found"})
            if ruta.endswith("conversations.history"):
                encontrados = [m for m in reversed(self.publicados) if not m["thread_ts"]]
            else:
                hilo = peticion.url.params["ts"]
                encontrados = [m for m in self.publicados if hilo in (m["ts"], m["thread_ts"])]
            con_metadatos = peticion.url.params.get("include_all_metadata") == "true"
            mensajes = []
            for m in encontrados:
                mensaje = {"type": "message", "ts": m["ts"], "text": m["text"]}
                if m["thread_ts"]:
                    mensaje["thread_ts"] = m["thread_ts"]
                if con_metadatos and m["metadata"]:
                    mensaje["metadata"] = m["metadata"]
                mensajes.append(mensaje)
            return httpx.Response(200, json={"ok": True, "messages": mensajes, "has_more": False})
        return httpx.Response(404)

    @property
    def mensajes(self) -> list[dict]:
        return [json.loads(p.content) for p in self.peticiones if p.url.path.endswith("chat.postMessage")]


class Reloj:
    """Reloj simulado para los vencimientos: reemplaza coordinacion._ahora."""

    def __init__(self, inicio: datetime):
        self.ahora = inicio

    def __call__(self) -> datetime:
        return self.ahora

    def avanzar(self, segundos: float) -> None:
        self.ahora += timedelta(seconds=segundos)


class Barrera:
    """Detiene una tarea con nombre la primera vez que el almacén llega a `momento` y se cumple `condicion`.

    Se agrega a `AlmacenMemoria.ganchos`. La prueba espera con `esperar()` a que la tarea llegue
    y la deja seguir con `soltar()`.
    """

    def __init__(self, tarea: str, momento: str = "aplicar", condicion=None):
        self.tarea = tarea
        self.momento = momento
        self.condicion = condicion
        self.llego = asyncio.Event()
        self.sigue = asyncio.Event()
        self.usada = False

    async def __call__(self, momento: str, *datos) -> None:
        actual = asyncio.current_task()
        if self.usada or momento != self.momento or actual is None or actual.get_name() != self.tarea:
            return
        if self.condicion is not None and not self.condicion(*datos):
            return
        self.usada = True
        self.llego.set()
        await self.sigue.wait()

    async def esperar(self) -> None:
        await asyncio.wait_for(self.llego.wait(), 2)

    def soltar(self) -> None:
        self.sigue.set()


def escribe_encargo(_condiciones, escrituras) -> bool:
    """Condición de barrera: el `aplicar` escribe el encargo."""
    return any(e[0] == "poner" and e[1] == ("encargo",) for e in escrituras)


def escribe_traspaso(_condiciones, escrituras) -> bool:
    """Condición de barrera: el `aplicar` guarda un traspaso (es decir, completa una operación)."""
    return any(e[0] == "poner" and e[1][0] == "traspaso" for e in escrituras)


async def como(cliente: str, herramienta, **argumentos):
    """Llama una herramienta de coordinación directamente, autenticado como `cliente`."""
    import coordinacion

    ficha = coordinacion.cliente_actual.set(cliente)
    try:
        return await herramienta(**argumentos)
    finally:
        coordinacion.cliente_actual.reset(ficha)


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
def reloj(monkeypatch):
    """Congela el reloj del board en 2026-11-23 12:00 UTC; la prueba lo adelanta a mano."""
    import coordinacion

    simulado = Reloj(datetime(2026, 11, 23, 12, 0, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(coordinacion, "_ahora", simulado)
    return simulado


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
