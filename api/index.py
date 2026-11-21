"""Servidor MCP del board de Itosturre (prueba de conexión, solo lectura).

Expone una sola herramienta, `obtener_tarea_instagram`, que devuelve un encargo
FICTICIO. Sirve para comprobar que el Itosturre Board Agent (Claude Managed
Agents) se conecta por Streamable HTTP antes de habilitar escritura en el board.

Seguridad:
- Autenticación bearer contra la variable de entorno MCP_TOKEN.
- Si MCP_TOKEN no está configurada, /mcp responde 503 (cerrado por defecto).
- No se registran tokens ni cabeceras en los logs.
"""

from __future__ import annotations

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


def _token_configurado() -> str:
    return os.environ.get("MCP_TOKEN", "").strip()


def _token_recibido(scope: Scope) -> str:
    for nombre, valor in scope.get("headers", []):
        if nombre == b"authorization":
            texto = valor.decode("latin-1").strip()
            if texto[:7].lower() == "bearer ":
                return texto[7:].strip()
    return ""


async def _responder_json(scope: Scope, receive: Receive, send: Send, estado: int, cuerpo: dict, cabeceras: dict | None = None) -> None:
    respuesta = JSONResponse(cuerpo, status_code=estado, headers=cabeceras)
    await respuesta(scope, receive, send)


class ManejadorMCP:
    """Valida el token y atiende cada petición con un gestor sin estado.

    Se crea un gestor por petición porque en serverless no hay garantía de que
    se ejecute el evento lifespan de ASGI que normalmente lo inicializa.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        esperado = _token_configurado()
        if not esperado:
            registro.error("MCP_TOKEN no está configurada; se rechaza la petición")
            await _responder_json(scope, receive, send, 503, {"error": "servidor_sin_configurar"})
            return

        recibido = _token_recibido(scope)
        if not recibido or not hmac.compare_digest(recibido.encode(), esperado.encode()):
            registro.warning("Petición rechazada: token ausente o inválido")
            await _responder_json(
                scope, receive, send, 401, {"error": "no_autorizado"}, {"WWW-Authenticate": 'Bearer realm="itosturre-mcp-board"'}
            )
            return

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
