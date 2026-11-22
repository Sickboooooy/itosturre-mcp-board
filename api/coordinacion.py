"""Herramientas de coordinación entre los agentes del board de Itosturre.

- obtener_encargo_vigente (lectura)
- registrar_traspaso (escritura): publica en Slack y actualiza el encargo
- registrar_acuse (escritura): el destinatario confirma en el hilo del traspaso
- listar_traspasos (lectura)

Reglas de las escrituras:
- `agente` debe coincidir con el token que llegó (salvo el cliente de pruebas,
  que solo existe fuera de Producción).
- `confirmar` debe ser true.
- `clave_idempotencia` evita mensajes duplicados en Slack.
- Ninguna herramienta publica, programa ni modifica Instagram.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Annotated, Literal
from urllib.parse import urlsplit

from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

import almacen as modulo_almacen
import publicador_slack

AgenteNombre = Literal["Muse", "Codex", "Claude", "Antigravity", "DeepSeek"]
Destinatario = Literal["Muse", "Codex", "Claude", "Antigravity", "DeepSeek", "Fabián"]

AGENTE_POR_CLIENTE = {
    "principal": "Claude",
    "codex": "Codex",
    "muse": "Muse",
    "antigravity": "Antigravity",
    "deepseek": "DeepSeek",
}
CLIENTE_PRUEBAS = "pruebas"

# Cliente autenticado de la petición en curso; lo fija index.py tras validar el token.
cliente_actual: ContextVar[str | None] = ContextVar("cliente_actual", default=None)

DOMINIOS_POR_DEFECTO = (
    "docs.google.com",
    "drive.google.com",
    "canva.com",
    "www.canva.com",
    "figma.com",
    "www.figma.com",
    "github.com",
)
SEGUNDOS_IDEMPOTENCIA = 30 * 24 * 3600
SEGUNDOS_EN_PROCESO = 120

PATRON_CLAVE = r"^[A-Za-z0-9_-]{8,64}$"
PATRON_ENCARGO = r"^[A-Z]{2,5}-\d{2,4}$"
PATRON_VERSION = r"^v\d{1,3}$"
PATRON_TRASPASO = r"^T-\d{8}-\d{6}-[0-9a-f]{4}$"


class ErrorBoard(ToolError):
    """Error previsto: el agente recibe {"error": código, "mensaje": texto}."""

    def __init__(self, codigo: str, mensaje: str):
        self.codigo = codigo
        self.mensaje = mensaje
        super().__init__(json.dumps({"error": codigo, "mensaje": mensaje}, ensure_ascii=False))


# --- dependencias (se reemplazan en las pruebas) ---
def entorno() -> str:
    return os.environ.get("VERCEL_ENV", "development")


def crear_almacen():
    return modulo_almacen.AlmacenUpstash.desde_entorno(entorno())


def crear_publicador():
    return publicador_slack.PublicadorSlack.desde_entorno()


# --- utilidades ---
def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def _iso(momento: datetime) -> str:
    return momento.isoformat(timespec="seconds").replace("+00:00", "Z")


def _nuevo_id_traspaso(momento: datetime) -> str:
    return f"T-{momento:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def _numero_version(version: str) -> int:
    return int(version[1:])


def _dominios_permitidos() -> frozenset[str]:
    valor = os.environ.get("ENLACES_DOMINIOS_PERMITIDOS", "")
    dominios = [d.strip().lower() for d in valor.split(",") if d.strip()]
    return frozenset(dominios or DOMINIOS_POR_DEFECTO)


def _validar_enlace(enlace: str) -> None:
    if re.search(r"[\s<>|]", enlace):
        raise ErrorBoard("enlace_invalido", "El enlace no puede tener espacios ni los caracteres < > |.")
    try:
        partes = urlsplit(enlace)
        puerto = partes.port
    except ValueError:
        raise ErrorBoard("enlace_invalido", "El enlace no es una URL válida.") from None
    if partes.scheme != "https" or not partes.hostname or partes.username or partes.password or puerto not in (None, 443):
        raise ErrorBoard("enlace_invalido", "El enlace debe ser https://, sin usuario, contraseña ni puerto.")
    if partes.hostname.lower() not in _dominios_permitidos():
        raise ErrorBoard("dominio_no_permitido", f"El dominio {partes.hostname} no está en la lista permitida.")


def _verificar_firma(agente: str) -> None:
    cliente = cliente_actual.get()
    if cliente is None:
        raise ErrorBoard("sin_identidad", "No se pudo identificar el cliente de esta petición.")
    if cliente == CLIENTE_PRUEBAS:
        return
    esperado = AGENTE_POR_CLIENTE.get(cliente)
    if esperado != agente:
        raise ErrorBoard("agente_no_coincide", f"Con este token solo puedes firmar como {esperado}.")


def _verificar_confirmacion(confirmar: bool) -> None:
    if confirmar is not True:
        raise ErrorBoard("confirmacion_requerida", "Para escribir, envía confirmar=true.")


async def _cerrar(almacen) -> None:
    cerrar = getattr(almacen, "cerrar", None)
    if cerrar:
        await cerrar()


async def _con_idempotencia(almacen, agente: str, clave: str, operacion) -> dict:
    """Ejecuta una escritura una sola vez por (agente, clave)."""
    previo = await almacen.leer_idempotencia(agente, clave)
    if previo is not None:
        if previo == modulo_almacen.EN_PROCESO:
            raise ErrorBoard("en_proceso", "Esa clave de idempotencia se está procesando. Reintenta en unos segundos.")
        datos = json.loads(previo)
        if "error" in datos:
            raise ErrorBoard(datos["error"], datos.get("mensaje", ""))
        return {**datos, "duplicado": True}
    if not await almacen.reservar_idempotencia(agente, clave, SEGUNDOS_EN_PROCESO):
        raise ErrorBoard("en_proceso", "Esa clave de idempotencia se está procesando. Reintenta en unos segundos.")
    try:
        resultado = await operacion()
    except ErrorBoard as error:
        if error.codigo == "registro_incompleto":
            # El mensaje ya salió a Slack: se guarda el error para no duplicarlo al reintentar.
            await almacen.guardar_idempotencia(
                agente, clave, json.dumps({"error": error.codigo, "mensaje": error.mensaje}, ensure_ascii=False), SEGUNDOS_IDEMPOTENCIA
            )
        else:
            await almacen.liberar_idempotencia(agente, clave)
        raise
    except Exception:
        await almacen.liberar_idempotencia(agente, clave)
        raise
    await almacen.guardar_idempotencia(agente, clave, json.dumps(resultado, ensure_ascii=False), SEGUNDOS_IDEMPOTENCIA)
    return {**resultado, "duplicado": False}


def _texto_traspaso(t: dict) -> str:
    esc = publicador_slack.escapar
    return (
        f"*Traspaso* {t['encargo_id']} {t['version']}: {esc(t['de'])} → {esc(t['para'])}\n"
        f"Enlace: <{t['enlace'].replace('&', '&amp;')}|abrir>\n"
        f"Nota: {esc(t['nota'])}\n"
        f"ID: `{t['id']}`"
    )


def _texto_acuse(traspaso: dict, agente: str, nota: str | None) -> str:
    esc = publicador_slack.escapar
    texto = f"*Acuse* de {esc(agente)}: recibí {traspaso['encargo_id']} {traspaso['version']}."
    if nota:
        texto += f"\nNota: {esc(nota)}"
    return texto


def _traducir_error_almacen(error: Exception) -> ErrorBoard:
    codigo = getattr(error, "codigo", "almacenamiento_no_disponible")
    return ErrorBoard(codigo, "El almacenamiento del board no está disponible. Reintenta más tarde.")


# --- herramientas ---
async def obtener_encargo_vigente() -> dict:
    """Devuelve el encargo vigente del board: ID, versión, estado, responsable siguiente, referencia y de qué fuente y fecha sale el dato. Solo lectura."""
    try:
        almacen = crear_almacen()
    except modulo_almacen.ErrorAlmacen as error:
        raise _traducir_error_almacen(error) from None
    try:
        encargo = await almacen.obtener_encargo()
    except modulo_almacen.ErrorAlmacen as error:
        raise _traducir_error_almacen(error) from None
    finally:
        await _cerrar(almacen)
    if encargo is None:
        raise ErrorBoard("sin_encargo_vigente", "Todavía no hay un encargo vigente cargado. Pídele a Fabián que lo cargue.")
    return encargo


async def registrar_traspaso(
    agente: Annotated[AgenteNombre, Field(description="Quién entrega. Debe coincidir con el agente de tu token.")],
    encargo_id: Annotated[str, Field(pattern=PATRON_ENCARGO, description="ID del encargo vigente, por ejemplo IG-01.")],
    version: Annotated[str, Field(pattern=PATRON_VERSION, description="Versión que entregas, por ejemplo v2. No puede ser anterior a la vigente.")],
    entrega_a: Annotated[Destinatario, Field(description="Quién recibe: otro agente o Fabián.")],
    enlace: Annotated[str, Field(min_length=12, max_length=500, description="Enlace https a la pieza, en un dominio permitido.")],
    nota: Annotated[str, Field(min_length=1, max_length=500, description="Qué entregas y qué falta. Sin datos de clientes.")],
    clave_idempotencia: Annotated[str, Field(pattern=PATRON_CLAVE, description="Clave única de 8 a 64 caracteres [A-Za-z0-9_-]. Si reintentas, usa la misma.")],
    confirmar: Annotated[bool, Field(description="Debe ser true para escribir.")],
) -> dict:
    """Registra que un agente entrega una versión del encargo vigente a otro agente o a Fabián. Publica el traspaso en el canal de Slack del board y devuelve su permalink. No publica nada en Instagram."""
    _verificar_confirmacion(confirmar)
    _verificar_firma(agente)
    if agente == entrega_a:
        raise ErrorBoard("entrega_a_si_mismo", "No puedes entregarte un traspaso a ti mismo.")
    _validar_enlace(enlace)

    try:
        almacen = crear_almacen()
    except modulo_almacen.ErrorAlmacen as error:
        raise _traducir_error_almacen(error) from None
    publicador = crear_publicador()

    async def _operacion() -> dict:
        encargo = await almacen.obtener_encargo()
        if encargo is None:
            raise ErrorBoard("sin_encargo_vigente", "Todavía no hay un encargo vigente cargado.")
        if encargo_id != encargo["id"]:
            raise ErrorBoard("encargo_distinto", f"El encargo vigente es {encargo['id']}, no {encargo_id}.")
        if _numero_version(version) < _numero_version(encargo["version"]):
            raise ErrorBoard("version_anterior", f"La versión vigente es {encargo['version']}; no se puede retroceder.")

        momento = _ahora()
        traspaso = {
            "id": _nuevo_id_traspaso(momento),
            "encargo_id": encargo_id,
            "version": version,
            "de": agente,
            "para": entrega_a,
            "enlace": enlace,
            "nota": nota,
            "registrado_en": _iso(momento),
            "slack_ts": None,
            "permalink": None,
            "acuse": None,
        }
        metadatos = {
            "event_type": "traspaso_registrado",
            "event_payload": {k: traspaso[k] for k in ("id", "encargo_id", "version", "de", "para")},
        }
        try:
            traspaso["slack_ts"] = await publicador.publicar(_texto_traspaso(traspaso), metadatos)
        except publicador_slack.ErrorSlack as error:
            raise ErrorBoard(error.codigo, "Slack rechazó la publicación; no se registró el traspaso.") from None
        traspaso["permalink"] = await publicador.enlace_permanente(traspaso["slack_ts"])

        nuevo_encargo = {
            **encargo,
            "version": version,
            "responsable_siguiente": entrega_a,
            "estado": "en_revision_fabian" if entrega_a == "Fabián" else "pendiente_acuse",
            "referencia": enlace,
            "actualizado_en": traspaso["registrado_en"],
            "fuente": {"tipo": "traspaso", "traspaso_id": traspaso["id"], "fecha": traspaso["registrado_en"]},
            "ultimo_traspaso_permalink": traspaso["permalink"],
        }
        try:
            await almacen.guardar_traspaso_y_encargo(traspaso, nuevo_encargo, nuevo=True)
        except modulo_almacen.ErrorAlmacen:
            raise ErrorBoard(
                "registro_incompleto",
                f"El mensaje se publicó en Slack ({traspaso['permalink'] or 'sin permalink'}) pero no se pudo guardar. Avísale a Fabián.",
            ) from None
        return {"traspaso_id": traspaso["id"], "permalink": traspaso["permalink"], "encargo": nuevo_encargo}

    try:
        return await _con_idempotencia(almacen, agente, clave_idempotencia, _operacion)
    except modulo_almacen.ErrorAlmacen as error:
        raise _traducir_error_almacen(error) from None
    finally:
        await _cerrar(almacen)


async def registrar_acuse(
    agente: Annotated[AgenteNombre, Field(description="Quién confirma. Debe ser el destinatario del traspaso y coincidir con tu token.")],
    traspaso_id: Annotated[str, Field(pattern=PATRON_TRASPASO, description="ID del traspaso, por ejemplo T-20261122-190512-a3f9.")],
    clave_idempotencia: Annotated[str, Field(pattern=PATRON_CLAVE, description="Clave única de 8 a 64 caracteres [A-Za-z0-9_-]. Si reintentas, usa la misma.")],
    confirmar: Annotated[bool, Field(description="Debe ser true para escribir.")],
    nota: Annotated[str | None, Field(max_length=300, description="Comentario opcional.")] = None,
) -> dict:
    """El destinatario de un traspaso confirma que lo recibió. Responde en el hilo de Slack del traspaso y pone el encargo en curso."""
    _verificar_confirmacion(confirmar)
    _verificar_firma(agente)

    try:
        almacen = crear_almacen()
    except modulo_almacen.ErrorAlmacen as error:
        raise _traducir_error_almacen(error) from None
    publicador = crear_publicador()

    async def _operacion() -> dict:
        traspaso = await almacen.obtener_traspaso(traspaso_id)
        if traspaso is None:
            raise ErrorBoard("traspaso_no_encontrado", f"No existe el traspaso {traspaso_id}.")
        if traspaso["para"] != agente:
            raise ErrorBoard("no_es_destinatario", f"Ese traspaso es para {traspaso['para']}.")
        if traspaso.get("acuse"):
            raise ErrorBoard("ya_confirmado", "Ese traspaso ya tiene acuse.")

        momento = _ahora()
        metadatos = {
            "event_type": "acuse_registrado",
            "event_payload": {"traspaso_id": traspaso_id, "de": agente},
        }
        try:
            ts = await publicador.publicar(_texto_acuse(traspaso, agente, nota), metadatos, hilo_ts=traspaso.get("slack_ts"))
        except publicador_slack.ErrorSlack as error:
            raise ErrorBoard(error.codigo, "Slack rechazó la publicación; no se registró el acuse.") from None
        permalink = await publicador.enlace_permanente(ts)

        traspaso["acuse"] = {"de": agente, "nota": nota, "registrado_en": _iso(momento), "permalink": permalink}
        encargo = await almacen.obtener_encargo()
        nuevo_encargo = None
        if encargo and encargo["id"] == traspaso["encargo_id"] and encargo["version"] == traspaso["version"]:
            nuevo_encargo = {**encargo, "estado": "en_curso", "actualizado_en": _iso(momento)}
        try:
            await almacen.guardar_traspaso_y_encargo(traspaso, nuevo_encargo, nuevo=False)
        except modulo_almacen.ErrorAlmacen:
            raise ErrorBoard(
                "registro_incompleto",
                f"El acuse se publicó en Slack ({permalink or 'sin permalink'}) pero no se pudo guardar. Avísale a Fabián.",
            ) from None
        return {"traspaso_id": traspaso_id, "permalink_respuesta": permalink}

    try:
        return await _con_idempotencia(almacen, agente, clave_idempotencia, _operacion)
    except modulo_almacen.ErrorAlmacen as error:
        raise _traducir_error_almacen(error) from None
    finally:
        await _cerrar(almacen)


async def listar_traspasos(
    n: Annotated[int, Field(ge=1, le=20, description="Cuántos traspasos devolver (1 a 20).")] = 10,
    encargo_id: Annotated[str | None, Field(pattern=PATRON_ENCARGO, description="Filtrar por encargo, opcional.")] = None,
) -> dict:
    """Lista los últimos traspasos y acuses, del más reciente al más antiguo, para que un agente se ponga al día antes de trabajar. Solo lectura."""
    try:
        almacen = crear_almacen()
    except modulo_almacen.ErrorAlmacen as error:
        raise _traducir_error_almacen(error) from None
    try:
        limite = modulo_almacen.MAXIMO_TRASPASOS if encargo_id else n
        traspasos = await almacen.listar_traspasos(limite)
    except modulo_almacen.ErrorAlmacen as error:
        raise _traducir_error_almacen(error) from None
    finally:
        await _cerrar(almacen)
    if encargo_id:
        traspasos = [t for t in traspasos if t.get("encargo_id") == encargo_id]
    return {"traspasos": traspasos[:n]}


def registrar(servidor) -> None:
    """Agrega las herramientas de coordinación al servidor MCP."""
    lectura = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    escritura = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)
    servidor.tool(title="Obtener encargo vigente", annotations=lectura)(obtener_encargo_vigente)
    servidor.tool(title="Registrar traspaso", annotations=escritura)(registrar_traspaso)
    servidor.tool(title="Registrar acuse", annotations=escritura)(registrar_acuse)
    servidor.tool(title="Listar traspasos", annotations=lectura)(listar_traspasos)
