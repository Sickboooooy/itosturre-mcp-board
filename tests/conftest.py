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
}
TOKEN_SLACK = "xoxb-secreto-de-prueba-NO-DEBE-SALIR"
CANAL_PRUEBAS = "C0CL2PM0UG1"

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


class AlmacenMemoria:
    """Mismo contrato que AlmacenUpstash, en memoria."""

    def __init__(self):
        self.encargo = None
        self.traspasos: dict[str, dict] = {}
        self.orden: list[str] = []
        self.idem: dict[str, str] = {}
        self.fallar_al_guardar = False

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
