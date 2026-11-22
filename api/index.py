"""Servidor MCP del board de Itosturre (prueba de conexión, solo lectura).

Expone una sola herramienta, `obtener_tarea_instagram`, que devuelve un encargo
FICTICIO. Sirve para comprobar que el Itosturre Board Agent (Claude Managed
Agents) se conecta por Streamable HTTP antes de habilitar escritura en el board.

Seguridad:
- Autenticación bearer con un token por cliente: MCP_TOKEN (Claude y Codex)
  y MCP_TOKEN_MUSE (Meta Muse). Los logs indican qué cliente entró.
- Si no hay ningún token configurado, /mcp responde 503 (cerrado por defecto).
- No se registran tokens ni cabeceras en los logs; ante un rechazo solo se
  registran longitudes y huellas SHA-256 truncadas para diagnosticar.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from datetime import datetime, timezone

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

NOMBRE_SERVICIO = "itosturre-mcp-board"
RUTA_MCP = "/mcp"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
registro = logging.getLogger("itosturre_mcp")

servidor = MCPServer(
    name=NOMBRE_SERVICIO,
    version="0.1.0",
    instructions=(
        "Servidor de prueba del board de Itosturre. Solo lectura. "
        "Los encargos que devuelve son ficticios y no contienen datos de clientes."
    ),
)


@servidor.tool(
    title="Obtener tarea de Instagram (ficticia)",
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
)
def obtener_tarea_instagram() -> dict:
    """Devuelve un encargo FICTICIO del board de Itosturre para probar la conexión MCP. No escribe nada."""
    registro.info("Herramienta obtener_tarea_instagram invocada")
    return {
        "id": "TAREA-PRUEBA-001",
        "ficticia": True,
        "titulo": "Prueba de conexión: variantes de biografía",
        "encargo": (
            "Proponer tres variantes de biografía de Instagram de máximo 150 caracteres "
            "para Itosturre, marcando como [POR CONFIRMAR] cualquier dato de trayectoria."
        ),
        "rol_sugerido": "Método y rigor",
        "restricciones": [
            "Solo borradores: no publicar, programar ni modificar el perfil.",
            "No usar datos de clientes ni expedientes.",
            "Este encargo es ficticio y existe solo para validar la conexión MCP.",
        ],
        "origen": NOMBRE_SERVICIO,
        "generado_en": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# La protección contra DNS rebinding está pensada para servidores en localhost.
# En Vercel el Host es el dominio público; el control de acceso lo da el token bearer.
SEGURIDAD_TRANSPORTE = TransportSecuritySettings(enable_dns_rebinding_protection=False)


# Un token por cliente, para poder revocar uno sin cortar a los demás.
# MCP_TOKEN lo comparten hoy Claude y Codex; MCP_TOKEN_MUSE es solo de Meta Muse.
VARIABLES_TOKEN = {
    "principal": "MCP_TOKEN",
    "muse": "MCP_TOKEN_MUSE",
}


def _tokens_configurados() -> dict[str, str]:
    tokens = {}
    for cliente, variable in VARIABLES_TOKEN.items():
        valor = os.environ.get(variable, "").strip()
        if valor:
            tokens[cliente] = valor
    return tokens


def _cabecera_autorizacion(scope: Scope) -> str | None:
    for nombre, valor in scope.get("headers", []):
        if nombre == b"authorization":
            return valor.decode("latin-1").strip()
    return None


def _token_recibido(scope: Scope) -> str:
    """Acepta 'Bearer <token>' y, por tolerancia, el token solo sin esquema."""
    texto = _cabecera_autorizacion(scope) or ""
    if texto[:7].lower() == "bearer ":
        return texto[7:].strip()
    if texto and " " not in texto:
        return texto
    return ""


def _cliente_autenticado(recibido: str, tokens: dict[str, str]) -> str | None:
    """Compara contra todos los tokens en tiempo constante y devuelve el cliente."""
    encontrado = None
    for cliente, esperado in tokens.items():
        if hmac.compare_digest(recibido.encode(), esperado.encode()):
            encontrado = cliente
    return encontrado


def _huella(valor: str) -> str:
    """Primeros 8 hex del SHA-256: permite comparar tokens sin revelarlos."""
    return hashlib.sha256(valor.encode()).hexdigest()[:8] if valor else "-"


def _diagnostico_rechazo(scope: Scope, recibido: str, tokens: dict[str, str]) -> str:
    cabecera = _cabecera_autorizacion(scope)
    if cabecera is None:
        return "sin cabecera Authorization"
    if not recibido:
        return "la cabecera Authorization no trae un token reconocible"
    esperados = ", ".join(f"{cliente}={len(valor)}/{_huella(valor)}" for cliente, valor in tokens.items())
    detalles = [
        f"recibido={len(recibido)}/{_huella(recibido)}",
        f"esperados (longitud/huella): {esperados}",
    ]
    if recibido[:7].lower() == "bearer ":
        detalles.append("posible doble prefijo 'Bearer'")
    if recibido[:1] in "\"'":
        detalles.append("el valor recibido empieza con comillas")
    return ", ".join(detalles)


async def _responder_json(scope: Scope, receive: Receive, send: Send, estado: int, cuerpo: dict, cabeceras: dict | None = None) -> None:
    respuesta = JSONResponse(cuerpo, status_code=estado, headers=cabeceras)
    await respuesta(scope, receive, send)


class ManejadorMCP:
    """Valida el token y atiende cada petición con un gestor sin estado.

    Se crea un gestor por petición porque en serverless no hay garantía de que
    se ejecute el evento lifespan de ASGI que normalmente lo inicializa.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        tokens = _tokens_configurados()
        if not tokens:
            registro.error("No hay ningún token configurado; se rechaza la petición")
            await _responder_json(scope, receive, send, 503, {"error": "servidor_sin_configurar"})
            return

        recibido = _token_recibido(scope)
        cliente = _cliente_autenticado(recibido, tokens) if recibido else None
        if cliente is None:
            registro.warning("Petición rechazada: %s", _diagnostico_rechazo(scope, recibido, tokens))
            await _responder_json(
                scope, receive, send, 401, {"error": "no_autorizado"}, {"WWW-Authenticate": 'Bearer realm="itosturre-mcp-board"'}
            )
            return

        registro.info("Cliente autenticado: %s", cliente)

        aplicacion = servidor.streamable_http_app(
            streamable_http_path=RUTA_MCP,
            json_response=True,
            stateless_http=True,
            transport_security=SEGURIDAD_TRANSPORTE,
            host="0.0.0.0",
        )
        gestor = servidor.session_manager
        async with gestor.run():
            await aplicacion(scope, receive, send)


async def salud(_peticion: Request) -> JSONResponse:
    return JSONResponse({"servicio": NOMBRE_SERVICIO, "estado": "ok", "ruta_mcp": RUTA_MCP})


app = Starlette(
    routes=[
        Route("/", salud, methods=["GET"]),
        Route(RUTA_MCP, ManejadorMCP(), methods=["GET", "POST", "DELETE"]),
    ]
)
