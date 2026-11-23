"""Herramientas de coordinación entre los agentes del board de Itosturre.

- obtener_encargo_vigente (lectura)
- registrar_traspaso (escritura): el responsable entrega una versión; se publica en Slack
- registrar_acuse (escritura): el destinatario del traspaso vigente confirma en el hilo
- listar_traspasos (lectura)

Reglas de las escrituras (IT-005):
- Identidad: `agente` debe coincidir con el token. El token de pruebas solo existe con
  VERCEL_ENV preview o development. El token opcional del propietario firma como Fabián.
- Un escritor: solo el responsable vigente, o Fabián, traspasan.
- Control de versión: cada transición sube `rev` en una sola operación atómica que compara
  el encargo leído (compare-and-set). La versión entregada es la siguiente (vN+1); repetirla
  o saltar exige `justificacion_version`; retroceder, nunca. `rev_esperada` es opcional.
- Idempotencia: la clave queda ligada a la operación y a la huella de sus argumentos. La
  reserva tiene dueño y vencimiento, se renueva, se recupera al vencer y se libera solo si
  sigue siendo del mismo dueño.
- Slack después de anotar: la operación se anota en el encargo («operacion_en_curso»)
  antes de publicar; nadie más escribe mientras tanto. Si Slack falla de forma definitiva, se
  descarta; si el resultado es incierto, se concilia buscando el mensaje por sus metadatos.
- `confirmar` debe ser true.
- Ninguna herramienta publica, programa ni modifica Instagram.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated, Callable, Literal
from urllib.parse import urlsplit

from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

import almacen as modulo_almacen
import publicador_slack
from almacen import ENCARGO, apilar_traspaso, borrar, clave_idem, clave_traspaso, poner

registro = logging.getLogger("itosturre_mcp.coordinacion")

PROPIETARIO = "Fabián"
AgenteNombre = Literal["Muse", "Codex", "Claude", "Antigravity", "DeepSeek", "Fabián"]
Destinatario = Literal["Muse", "Codex", "Claude", "Antigravity", "DeepSeek", "Fabián"]

AGENTE_POR_CLIENTE = {
    "principal": "Claude",
    "codex": "Codex",
    "muse": "Muse",
    "antigravity": "Antigravity",
    "deepseek": "DeepSeek",
    # Token opcional del propietario (MCP_TOKEN_PROPIETARIO): firma como Fabián.
    "propietario": PROPIETARIO,
}
CLIENTE_PRUEBAS = "pruebas"
# El token de pruebas firma como cualquier agente: solo se acepta si VERCEL_ENV es exactamente uno
# de estos valores. Si la variable falta, está vacía o trae otro valor (incluido «Production»), queda cerrado.
ENTORNOS_DE_PRUEBA = frozenset({"preview", "development"})
# Clientes con token propio que solo pueden leer: no tienen nombre de firma.
CLIENTES_SOLO_LECTURA = frozenset({"openai"})

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
SEGUNDOS_IDEMPOTENCIA = 30 * 24 * 3600  # cuánto se recuerda una clave
SEGUNDOS_RESERVA = 120  # reserva de la clave mientras se decide; se renueva en cada reintento
SEGUNDOS_OPERACION = 120  # la operación anotada bloquea el encargo mientras se publica
SEGUNDOS_PLAZO_SLACK = 30  # tope de la publicación: termina mucho antes de que venza la operación
SEGUNDOS_PLAZO_PERMALINK = 10
SEGUNDOS_MARGEN_BUSQUEDA = 60  # la conciliación busca desde un poco antes de anotar la operación
INTENTOS = 5  # compare-and-set perdidos antes de rendirse

PATRON_CLAVE = r"^[A-Za-z0-9_-]{8,64}$"
PATRON_ENCARGO = r"^[A-Z]{2,5}-\d{2,4}$"
PATRON_VERSION = r"^v\d{1,3}$"
PATRON_TRASPASO = r"^T-\d{8}-\d{6}-[0-9a-f]{4}$"

NOMBRE_OPERACION = {"traspaso": "traspaso", "acuse": "acuse"}
EVENTO_SLACK = {"traspaso": "traspaso_registrado", "acuse": "acuse_registrado"}


class ErrorBoard(ToolError):
    """Error previsto: el agente recibe {"error": código, "mensaje": texto}."""

    def __init__(self, codigo: str, mensaje: str):
        self.codigo = codigo
        self.mensaje = mensaje
        super().__init__(json.dumps({"error": codigo, "mensaje": mensaje}, ensure_ascii=False))


# --- dependencias (se reemplazan en las pruebas) ---
def entorno() -> str:
    return os.environ.get("VERCEL_ENV", "development")


def token_de_pruebas_permitido() -> bool:
    """Lista permitida y comparación exacta: lo desconocido queda cerrado."""
    return os.environ.get("VERCEL_ENV") in ENTORNOS_DE_PRUEBA


def crear_almacen():
    return modulo_almacen.AlmacenUpstash.desde_entorno(entorno())


def crear_publicador():
    return publicador_slack.PublicadorSlack.desde_entorno()


# --- utilidades ---
def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def _iso(momento: datetime) -> str:
    return momento.isoformat(timespec="seconds").replace("+00:00", "Z")


def _leer_iso(texto: str) -> datetime:
    return datetime.fromisoformat(texto.replace("Z", "+00:00"))


def _vencido(vence_en: str | None, ahora: datetime) -> bool:
    return vence_en is not None and _leer_iso(vence_en) <= ahora


def _nuevo_id_traspaso(momento: datetime) -> str:
    return f"T-{momento:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def _nuevo_id_acuse(momento: datetime) -> str:
    return f"A-{momento:%Y%m%d-%H%M%S}-{secrets.token_hex(4)}"


def _numero_version(version: str) -> int:
    return int(version[1:])


def _rev(encargo: dict) -> int:
    return int(encargo.get("rev", 0))


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
        # Segunda barrera: index.py ya no carga el token fuera de un entorno de prueba.
        if not token_de_pruebas_permitido():
            raise ErrorBoard("token_de_pruebas_no_permitido", "El token de pruebas no se acepta en este entorno.")
        return
    esperado = AGENTE_POR_CLIENTE.get(cliente)
    if esperado is None:
        raise ErrorBoard("solo_lectura", "Este token es de solo lectura: no puede registrar traspasos ni acuses.")
    if esperado != agente:
        raise ErrorBoard("agente_no_coincide", f"Con este token solo puedes firmar como {esperado}.")


def _verificar_confirmacion(confirmar: bool) -> None:
    if confirmar is not True:
        raise ErrorBoard("confirmacion_requerida", "Para escribir, envía confirmar=true.")


async def _cerrar(almacen) -> None:
    cerrar = getattr(almacen, "cerrar", None)
    if cerrar:
        await cerrar()


def _traducir_error_almacen(error: Exception) -> ErrorBoard:
    codigo = getattr(error, "codigo", "almacenamiento_no_disponible")
    return ErrorBoard(codigo, "El almacenamiento del board no está disponible. Reintenta más tarde.")


# --- reglas sobre el encargo ---
def _verificar_encargo(encargo: dict | None, encargo_id: str) -> None:
    if encargo is None:
        raise ErrorBoard("sin_encargo_vigente", "Todavía no hay un encargo vigente cargado.")
    if encargo_id != encargo.get("id"):
        raise ErrorBoard("encargo_distinto", f"El encargo vigente es {encargo.get('id')}, no {encargo_id}.")


def _verificar_rev(encargo: dict, rev_esperada: int | None) -> None:
    if rev_esperada is not None and rev_esperada != _rev(encargo):
        raise ErrorBoard("conflicto_revision", f"La revisión vigente del encargo es {_rev(encargo)}, no {rev_esperada}; vuelve a leerlo.")


def _verificar_escritor(encargo: dict, agente: str, ahora: datetime) -> None:
    """Un escritor: el responsable vigente o el propietario."""
    if agente == PROPIETARIO:
        return
    responsable = encargo.get("responsable_siguiente")
    if agente != responsable:
        raise ErrorBoard("no_es_responsable", f"El responsable vigente es {responsable}; solo el responsable o Fabián pueden traspasar.")


def _verificar_version(encargo: dict, version: str, justificacion: str | None) -> None:
    """Control de versión estricto: la siguiente; repetir o saltar solo con justificación; nunca hacia atrás."""
    vigente = str(encargo.get("version", ""))
    if not re.fullmatch(PATRON_VERSION, vigente):
        raise ErrorBoard("encargo_invalido", "La versión del encargo vigente no es válida. Avísale a Fabián.")
    actual, nueva = _numero_version(vigente), _numero_version(version)
    if nueva < actual:
        raise ErrorBoard("version_anterior", f"La versión vigente es {vigente}; no se puede retroceder.")
    if justificacion:
        return
    if nueva == actual:
        raise ErrorBoard(
            "version_repetida", f"La versión vigente ya es {vigente}: entrega v{actual + 1} o explica en justificacion_version por qué la repites."
        )
    if nueva > actual + 1:
        raise ErrorBoard("version_salto", f"La siguiente versión es v{actual + 1}: para pasar a {version}, explica el salto en justificacion_version.")


def _traspaso_vigente(encargo: dict) -> str | None:
    """Traspaso que espera acuse. Los encargos anteriores a IT-005 lo tienen solo en `fuente`."""
    if "traspaso_vigente" in encargo:
        return encargo["traspaso_vigente"]
    fuente = encargo.get("fuente") or {}
    return fuente.get("traspaso_id") if fuente.get("tipo") == "traspaso" else None


def _sin_operacion(encargo: dict) -> dict:
    return {k: v for k, v in encargo.items() if k != "operacion_en_curso"}


def _encargo_tras_traspaso(base: dict, traspaso: dict) -> dict:
    para = traspaso["para"]
    return {
        **base,
        "version": traspaso["version"],
        "responsable_siguiente": para,
        "estado": "en_revision_fabian" if para == PROPIETARIO else "pendiente_acuse",
        "referencia": traspaso["enlace"],
        # La nota del encargo siempre es la del último traspaso, para no arrastrar estados viejos.
        "nota": traspaso["nota"],
        "actualizado_en": traspaso["registrado_en"],
        "fuente": {"tipo": "traspaso", "traspaso_id": traspaso["id"], "fecha": traspaso["registrado_en"]},
        "ultimo_traspaso_permalink": traspaso["permalink"],
        "rev": _rev(base) + 1,
        "traspaso_vigente": traspaso["id"],
    }


def _encargo_tras_acuse(base: dict, acuse: dict, ahora: datetime) -> dict:
    return {**base, "estado": "en_curso", "actualizado_en": _iso(ahora), "rev": _rev(base) + 1}


def _vista_encargo(encargo: dict) -> dict:
    """Lo que ven los agentes: el encargo, su rev y un resumen de la operación en curso."""
    vista = _sin_operacion(encargo)
    vista.setdefault("rev", 0)
    operacion = encargo.get("operacion_en_curso")
    if operacion:
        vista["operacion_en_curso"] = {k: operacion.get(k) for k in ("tipo", "id", "agente", "vence_en")}
    return vista


# --- mensajes de Slack ---
def _texto_traspaso(t: dict) -> str:
    esc = publicador_slack.escapar
    texto = (
        f"*Traspaso* {t['encargo_id']} {t['version']}: {esc(t['de'])} → {esc(t['para'])}\n"
        f"Enlace: <{t['enlace'].replace('&', '&amp;')}|abrir>\n"
        f"Nota: {esc(t['nota'])}\n"
    )
    if t.get("justificacion_version"):
        texto += f"Justificación de versión: {esc(t['justificacion_version'])}\n"
    return texto + f"ID: `{t['id']}`"


def _texto_acuse(operacion: dict) -> str:
    esc = publicador_slack.escapar
    acuse = operacion["acuse"]
    texto = f"*Acuse* de {esc(acuse['de'])}: recibí {operacion['encargo_id']} {operacion['version']}."
    if acuse.get("nota"):
        texto += f"\nNota: {esc(acuse['nota'])}"
    return texto


def _mensaje(operacion: dict) -> tuple[str, dict]:
    if operacion["tipo"] == "traspaso":
        t = operacion["traspaso"]
        payload = {k: t[k] for k in ("id", "encargo_id", "version", "de", "para")}
        return _texto_traspaso(t), {"event_type": EVENTO_SLACK["traspaso"], "event_payload": payload}
    acuse = operacion["acuse"]
    payload = {"id": acuse["id"], "traspaso_id": operacion["traspaso_id"], "de": acuse["de"]}
    return _texto_acuse(operacion), {"event_type": EVENTO_SLACK["acuse"], "event_payload": payload}


async def _permalink(publicador, ts: str | None) -> str | None:
    if not ts:
        return None
    try:
        return await asyncio.wait_for(publicador.enlace_permanente(ts), SEGUNDOS_PLAZO_PERMALINK)
    except TimeoutError:
        return None


# --- idempotencia: clave ligada a la operación y a sus argumentos ---
@dataclass
class _Reserva:
    """Reserva de una clave de idempotencia: el valor guardado y su marca (dueño incluido)."""

    clave: tuple
    valor: dict
    marca: str


@dataclass
class _Plan:
    """Lo que decidió una escritura sobre la foto leída.

    - `operacion`: datos de una operación que se anota en el encargo y se publica en Slack.
    - `encargo` y `resultado`: escritura directa, sin Slack.
    """

    operacion: dict | None = None
    encargo: dict | None = None
    resultado: dict | None = None


def _huella(operacion: str, argumentos: dict) -> str:
    texto = json.dumps({"operacion": operacion, "argumentos": argumentos}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def _error_en_proceso() -> ErrorBoard:
    return ErrorBoard("en_proceso", "Esa clave de idempotencia se está procesando. Reintenta en unos segundos.")


def _verificar_vinculo(idem, operacion: str, huella: str) -> None:
    """La clave solo vale para la misma operación con los mismos argumentos."""
    if idem == modulo_almacen.EN_PROCESO:
        raise _error_en_proceso()  # reserva anterior a IT-005: Redis la borra sola a los 120 s
    if not isinstance(idem, dict) or "huella" not in idem:
        raise ErrorBoard("clave_reutilizada", "Esa clave de idempotencia se usó antes de la versión 0.3.0 del board; usa una clave nueva.")
    if idem.get("operacion") != operacion or idem.get("huella") != huella:
        raise ErrorBoard(
            "clave_reutilizada",
            "Esa clave de idempotencia ya se usó con otra operación o con otros datos. Si reintentas, envía exactamente "
            "los mismos datos; si es otra escritura, usa una clave nueva.",
        )


def _vinculo(reserva: _Reserva) -> dict:
    return {"operacion": reserva.valor["operacion"], "huella": reserva.valor["huella"]}


async def _renovar_reserva(almacen, reserva: _Reserva) -> bool:
    """Extiende la reserva si sigue siendo nuestra (compare-and-set sobre su marca)."""
    nuevo = {**reserva.valor, "vence_en": _iso(_ahora() + timedelta(seconds=SEGUNDOS_RESERVA))}
    if not await almacen.aplicar([(reserva.clave, reserva.marca)], [poner(reserva.clave, nuevo, SEGUNDOS_IDEMPOTENCIA)]):
        return False
    reserva.valor, reserva.marca = nuevo, almacen.marca(nuevo)
    return True


async def _liberar_reserva(almacen, reserva: _Reserva) -> None:
    """Liberación condicionada: borra la reserva solo si sigue siendo nuestra y sigue en proceso."""
    try:
        await almacen.aplicar([(reserva.clave, reserva.marca)], [borrar(reserva.clave)])
    except Exception:
        registro.warning("No se pudo liberar una reserva de idempotencia; vencerá sola")


# --- operaciones anotadas en el encargo y publicadas en Slack ---
async def _completar(almacen, bloqueado: dict, marca: str, ts: str | None, permalink: str | None, estado_publicacion: str) -> dict | None:
    """Aplica la transición anotada. Devuelve el resultado, o None si otro ya la resolvió."""
    operacion = bloqueado["operacion_en_curso"]
    base = _sin_operacion(bloqueado)
    ahora = _ahora()
    publicacion = {"estado": estado_publicacion, "ts": ts}
    condiciones = [(ENCARGO, marca)]
    nombre = NOMBRE_OPERACION[operacion["tipo"]]
    try:
        if operacion["tipo"] == "traspaso":
            traspaso = {**operacion["traspaso"], "slack_ts": ts, "permalink": permalink, "publicacion": publicacion}
            nuevo = _encargo_tras_traspaso(base, traspaso)
            resultado = {"traspaso_id": traspaso["id"], "permalink": permalink, "encargo": _vista_encargo(nuevo)}
            escrituras = [poner(clave_traspaso(traspaso["id"]), traspaso), apilar_traspaso(traspaso["id"])]
        else:
            k_traspaso = clave_traspaso(operacion["traspaso_id"])
            [(traspaso, marca_traspaso)] = await almacen.leer(k_traspaso)
            if not isinstance(traspaso, dict):
                raise ErrorBoard("traspaso_no_encontrado", f"No existe el traspaso {operacion['traspaso_id']}.")
            acuse = {**operacion["acuse"], "permalink": permalink, "publicacion": publicacion}
            traspaso = {**traspaso, "acuse": acuse}
            nuevo = _encargo_tras_acuse(base, acuse, ahora)
            resultado = {"traspaso_id": traspaso["id"], "permalink_respuesta": permalink}
            condiciones.append((k_traspaso, marca_traspaso))
            escrituras = [poner(k_traspaso, traspaso)]
        completado = {"operacion": operacion["operacion"], "huella": operacion["huella"], "estado": "completado", "resultado": resultado}
        escrituras += [
            poner(ENCARGO, nuevo),
            poner(clave_idem(operacion["agente"], operacion["clave_idempotencia"]), completado, SEGUNDOS_IDEMPOTENCIA),
        ]
        aplicado = await almacen.aplicar(condiciones, escrituras)
    except modulo_almacen.ErrorAlmacen:
        donde = f"se publicó en Slack ({permalink or 'sin permalink'})" if ts else "quedó pendiente de confirmar en Slack"
        raise ErrorBoard(
            "registro_incompleto",
            f"El {nombre} {operacion['id']} {donde} pero no se pudo guardar. Reintenta con la misma clave para conciliarlo.",
        ) from None
    return resultado if aplicado else None


async def _descartar(almacen, bloqueado: dict, marca: str) -> bool:
    """Deshace una operación anotada que no llegó a Slack: el encargo vuelve a como estaba y la clave queda libre."""
    operacion = bloqueado["operacion_en_curso"]
    k_idem = clave_idem(operacion["agente"], operacion["clave_idempotencia"])
    return await almacen.aplicar([(ENCARGO, marca)], [poner(ENCARGO, _sin_operacion(bloqueado)), borrar(k_idem)])


async def _conciliar(publicador, operacion: dict) -> tuple[str, str | None]:
    """¿Llegó a Slack? ("publicado", ts), ("ausente", None) o ("desconocido", None)."""
    desde = _leer_iso(operacion["creado_en"]) - timedelta(seconds=SEGUNDOS_MARGEN_BUSQUEDA)
    try:
        ts = await asyncio.wait_for(
            publicador.buscar_evento(EVENTO_SLACK[operacion["tipo"]], operacion["id"], hilo_ts=operacion.get("hilo_ts"), desde=desde.timestamp()),
            SEGUNDOS_PLAZO_SLACK,
        )
    except (publicador_slack.ErrorSlack, TimeoutError):
        return "desconocido", None
    return ("publicado", ts) if ts else ("ausente", None)


async def _resolver_operacion(almacen, publicador, encargo: dict, marca: str, propia: bool) -> dict | None:
    """Resuelve una operación anotada en el encargo.

    - Ajena y vigente: se espera (operacion_en_curso).
    - Si el mensaje está en Slack: se completa (en cualquier momento; encontrarlo siempre es seguro).
    - Vigente y sin mensaje: se espera (la publicación puede seguir en curso).
    - Vencida y sin mensaje: se descarta.
    - Vencida y sin poder consultar Slack: se aplica sin volver a publicar (publicacion no_confirmada).
    Devuelve el resultado si la completó, o None si la descartó o si otro la resolvió antes.
    """
    operacion = encargo["operacion_en_curso"]
    vencida = _vencido(operacion.get("vence_en"), _ahora())
    nombre = NOMBRE_OPERACION.get(operacion.get("tipo"), "registro")
    if not propia and not vencida:
        raise ErrorBoard("operacion_en_curso", f"{operacion.get('agente')} está registrando un {nombre}; reintenta en unos segundos.")
    estado, ts = await _conciliar(publicador, operacion)
    if estado == "publicado":
        return await _completar(almacen, encargo, marca, ts, await _permalink(publicador, ts), "publicado")
    if not vencida:
        raise _error_en_proceso()
    if estado == "ausente":
        registro.warning("Se descarta el %s %s: no llegó a Slack", nombre, operacion["id"])
        await _descartar(almacen, encargo, marca)
        return None
    registro.warning("Se aplica el %s %s sin confirmar su publicación en Slack", nombre, operacion["id"])
    return await _completar(almacen, encargo, marca, None, None, "no_confirmada")


async def _resultado_guardado(almacen, operacion: dict) -> dict:
    [(idem, _)] = await almacen.leer(clave_idem(operacion["agente"], operacion["clave_idempotencia"]))
    if isinstance(idem, dict) and idem.get("estado") == "completado":
        return idem["resultado"]
    raise ErrorBoard("registro_incompleto", f"No se pudo confirmar el registro de {operacion['id']}. Reintenta con la misma clave.")


async def _publicar_y_completar(almacen, publicador, bloqueado: dict, marca: str) -> dict:
    operacion = bloqueado["operacion_en_curso"]
    nombre = NOMBRE_OPERACION[operacion["tipo"]]
    texto, metadatos = _mensaje(operacion)
    incierto = ErrorBoard(
        "publicacion_incierta",
        f"No se sabe si Slack recibió el {nombre} {operacion['id']}. Reintenta con la misma clave: el board lo concilia sin duplicarlo.",
    )
    try:
        ts = await asyncio.wait_for(publicador.publicar(texto, metadatos, hilo_ts=operacion.get("hilo_ts")), SEGUNDOS_PLAZO_SLACK)
    except publicador_slack.ErrorSlack as error:
        if error.incierto:
            raise incierto from None
        try:
            await _descartar(almacen, bloqueado, marca)
        except modulo_almacen.ErrorAlmacen:
            registro.warning("No se pudo descartar el %s %s; se descartará al vencer", nombre, operacion["id"])
        raise ErrorBoard(error.codigo, f"Slack rechazó la publicación; no se registró el {nombre}.") from None
    except TimeoutError:
        raise incierto from None
    resultado = await _completar(almacen, bloqueado, marca, ts, await _permalink(publicador, ts), "publicado")
    if resultado is None:
        # Otra petición con la misma clave encontró el mensaje y la completó primero.
        resultado = await _resultado_guardado(almacen, operacion)
    return resultado


# --- motor de las escrituras ---
async def _ejecutar_con_reserva(almacen, publicador, reserva: _Reserva, decidir: Callable, extras: tuple) -> dict | None:
    """Lee, decide y escribe con compare-and-set. Devuelve None si perdió la reserva."""
    for intento in range(INTENTOS):
        if intento and not await _renovar_reserva(almacen, reserva):
            return None
        leidos = await almacen.leer(ENCARGO, *extras)
        encargo, marca_encargo = leidos[0]
        if encargo is not None and not isinstance(encargo, dict):
            raise ErrorBoard("encargo_invalido", "El encargo vigente no es válido. Avísale a Fabián.")
        if encargo is not None and encargo.get("operacion_en_curso"):
            await _resolver_operacion(almacen, publicador, encargo, marca_encargo, propia=False)
            continue
        ahora = _ahora()
        plan = decidir(encargo, {clave: valor for clave, (valor, _) in zip(extras, leidos[1:])}, ahora)
        condiciones = [(ENCARGO, marca_encargo), (reserva.clave, reserva.marca)]
        if plan.operacion is None:
            completado = {**_vinculo(reserva), "estado": "completado", "resultado": plan.resultado}
            escrituras = [poner(ENCARGO, plan.encargo), poner(reserva.clave, completado, SEGUNDOS_IDEMPOTENCIA)]
            if await almacen.aplicar(condiciones, escrituras):
                return plan.resultado
            continue
        try:
            publicador.verificar()
        except publicador_slack.ErrorSlack as error:
            raise ErrorBoard(error.codigo, "Slack no está listo para publicar; no se registró nada.") from None
        _, agente, clave = reserva.clave
        operacion = {
            **plan.operacion,
            "agente": agente,
            "clave_idempotencia": clave,
            **_vinculo(reserva),
            "creado_en": _iso(ahora),
            "vence_en": _iso(ahora + timedelta(seconds=SEGUNDOS_OPERACION)),
        }
        bloqueado = {**encargo, "operacion_en_curso": operacion}
        registrado = {**_vinculo(reserva), "estado": "registrado", "evento_id": operacion["id"]}
        if await almacen.aplicar(condiciones, [poner(ENCARGO, bloqueado), poner(reserva.clave, registrado, SEGUNDOS_IDEMPOTENCIA)]):
            return await _publicar_y_completar(almacen, publicador, bloqueado, almacen.marca(bloqueado))
    raise ErrorBoard("conflicto_concurrente", "Hubo demasiadas escrituras simultáneas; vuelve a leer el encargo y reintenta.")


async def _continuar(almacen, publicador, idem: dict, k_idem: tuple) -> dict | None:
    """Reintento de una operación ya anotada. Devuelve el resultado, o None para volver a leer la clave."""
    [(encargo, marca)] = await almacen.leer(ENCARGO)
    operacion = encargo.get("operacion_en_curso") if isinstance(encargo, dict) else None
    if operacion and operacion.get("id") == idem.get("evento_id"):
        return await _resolver_operacion(almacen, publicador, encargo, marca, propia=True)
    [(actual, _)] = await almacen.leer(k_idem)
    if isinstance(actual, dict) and actual.get("estado") == "completado":
        return actual["resultado"]
    if actual is None or (isinstance(actual, dict) and actual.get("estado") != "registrado"):
        return None
    raise ErrorBoard(
        "operacion_descartada", "La operación de esta clave quedó sin efecto porque el encargo cambió por fuera del board. Usa una clave nueva."
    )


async def _ejecutar_escritura(almacen, publicador, operacion: str, agente: str, clave: str, argumentos: dict, decidir: Callable, extras: tuple = ()) -> dict:
    """Ejecuta una escritura una sola vez por (agente, clave), ligada a la operación y a sus argumentos."""
    huella = _huella(operacion, argumentos)
    k_idem = clave_idem(agente, clave)
    for _ in range(INTENTOS):
        [(idem, marca_idem)] = await almacen.leer(k_idem)
        if idem is not None:
            _verificar_vinculo(idem, operacion, huella)
            if idem.get("estado") == "completado":
                return {**idem["resultado"], "duplicado": True}
            if idem.get("estado") == "registrado":
                resultado = await _continuar(almacen, publicador, idem, k_idem)
                if resultado is not None:
                    return {**resultado, "duplicado": True}
                continue
            if not _vencido(idem.get("vence_en"), _ahora()):
                raise _error_en_proceso()
            registro.warning("Se recupera una reserva de idempotencia vencida (%s)", operacion)
        valor = {
            "operacion": operacion,
            "huella": huella,
            "estado": "en_proceso",
            "dueno": secrets.token_hex(8),
            "vence_en": _iso(_ahora() + timedelta(seconds=SEGUNDOS_RESERVA)),
        }
        if not await almacen.aplicar([(k_idem, marca_idem)], [poner(k_idem, valor, SEGUNDOS_IDEMPOTENCIA)]):
            continue
        reserva = _Reserva(k_idem, valor, almacen.marca(valor))
        try:
            resultado = await _ejecutar_con_reserva(almacen, publicador, reserva, decidir, extras)
        except BaseException:
            await _liberar_reserva(almacen, reserva)
            raise
        if resultado is not None:
            return {**resultado, "duplicado": False}
    raise ErrorBoard("conflicto_concurrente", "Hubo demasiadas escrituras simultáneas con esta clave; reintenta en unos segundos.")


async def _escribir(operacion: str, agente: str, clave: str, argumentos: dict, decidir: Callable, extras: tuple = ()) -> dict:
    try:
        almacen = crear_almacen()
    except modulo_almacen.ErrorAlmacen as error:
        raise _traducir_error_almacen(error) from None
    publicador = crear_publicador()
    try:
        return await _ejecutar_escritura(almacen, publicador, operacion, agente, clave, argumentos, decidir, extras)
    except modulo_almacen.ErrorAlmacen as error:
        raise _traducir_error_almacen(error) from None
    finally:
        await _cerrar(almacen)


# --- herramientas ---
async def obtener_encargo_vigente() -> dict:
    """Devuelve el encargo vigente del board: ID, versión, revisión (rev), estado, responsable siguiente, referencia y de qué fuente y fecha sale el dato. Solo lectura."""
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
    return _vista_encargo(encargo)


async def registrar_traspaso(
    agente: Annotated[AgenteNombre, Field(description="Quién entrega. Debe coincidir con el agente de tu token y ser el responsable vigente (o Fabián).")],
    encargo_id: Annotated[str, Field(pattern=PATRON_ENCARGO, description="ID del encargo vigente, por ejemplo IG-01.")],
    version: Annotated[str, Field(pattern=PATRON_VERSION, description="Versión que entregas: la siguiente a la vigente (v2 si la vigente es v1).")],
    entrega_a: Annotated[Destinatario, Field(description="Quién recibe: otro agente o Fabián.")],
    enlace: Annotated[str, Field(min_length=12, max_length=500, description="Enlace https a la pieza, en un dominio permitido.")],
    nota: Annotated[str, Field(min_length=1, max_length=500, description="Qué entregas y qué falta. Sin datos de clientes.")],
    clave_idempotencia: Annotated[str, Field(pattern=PATRON_CLAVE, description="Clave única de 8 a 64 caracteres [A-Za-z0-9_-]. Si reintentas, usa la misma con los mismos datos.")],
    confirmar: Annotated[bool, Field(description="Debe ser true para escribir.")],
    justificacion_version: Annotated[
        str | None, Field(min_length=1, max_length=300, description="Solo si repites la versión vigente o saltas versiones: por qué.")
    ] = None,
    rev_esperada: Annotated[int | None, Field(ge=0, description="Opcional: la rev del encargo que leíste; si cambió, no se escribe.")] = None,
) -> dict:
    """Registra que el responsable vigente entrega una versión del encargo a otro agente o a Fabián. Primero lo anota en el board y después lo publica en el canal de Slack; devuelve su permalink. No publica nada en Instagram."""
    _verificar_confirmacion(confirmar)
    _verificar_firma(agente)
    if agente == entrega_a:
        raise ErrorBoard("entrega_a_si_mismo", "No puedes entregarte un traspaso a ti mismo.")
    _validar_enlace(enlace)
    argumentos = {
        "agente": agente,
        "encargo_id": encargo_id,
        "version": version,
        "entrega_a": entrega_a,
        "enlace": enlace,
        "nota": nota,
        "justificacion_version": justificacion_version,
        "rev_esperada": rev_esperada,
    }

    def decidir(encargo: dict | None, _extras: dict, ahora: datetime) -> _Plan:
        _verificar_encargo(encargo, encargo_id)
        _verificar_rev(encargo, rev_esperada)
        _verificar_escritor(encargo, agente, ahora)
        _verificar_version(encargo, version, justificacion_version)
        traspaso = {
            "id": _nuevo_id_traspaso(ahora),
            "encargo_id": encargo_id,
            "version": version,
            "de": agente,
            "para": entrega_a,
            "enlace": enlace,
            "nota": nota,
            "registrado_en": _iso(ahora),
            "slack_ts": None,
            "permalink": None,
            "acuse": None,
        }
        if justificacion_version:
            traspaso["justificacion_version"] = justificacion_version
        return _Plan(operacion={"tipo": "traspaso", "id": traspaso["id"], "traspaso": traspaso, "hilo_ts": None})

    return await _escribir("registrar_traspaso", agente, clave_idempotencia, argumentos, decidir)


async def registrar_acuse(
    agente: Annotated[AgenteNombre, Field(description="Quién confirma. Debe ser el destinatario del traspaso y coincidir con tu token.")],
    traspaso_id: Annotated[str, Field(pattern=PATRON_TRASPASO, description="ID del traspaso vigente, por ejemplo T-20261122-190512-a3f9.")],
    clave_idempotencia: Annotated[str, Field(pattern=PATRON_CLAVE, description="Clave única de 8 a 64 caracteres [A-Za-z0-9_-]. Si reintentas, usa la misma con los mismos datos.")],
    confirmar: Annotated[bool, Field(description="Debe ser true para escribir.")],
    nota: Annotated[str | None, Field(max_length=300, description="Comentario opcional.")] = None,
    rev_esperada: Annotated[int | None, Field(ge=0, description="Opcional: la rev del encargo que leíste; si cambió, no se escribe.")] = None,
) -> dict:
    """El destinatario del traspaso vigente confirma que lo recibió. Se anota en el board, se responde en el hilo de Slack del traspaso y el encargo queda en curso."""
    _verificar_confirmacion(confirmar)
    _verificar_firma(agente)
    argumentos = {"agente": agente, "traspaso_id": traspaso_id, "nota": nota, "rev_esperada": rev_esperada}
    k_traspaso = clave_traspaso(traspaso_id)

    def decidir(encargo: dict | None, extras: dict, ahora: datetime) -> _Plan:
        traspaso = extras[k_traspaso]
        if not isinstance(traspaso, dict):
            raise ErrorBoard("traspaso_no_encontrado", f"No existe el traspaso {traspaso_id}.")
        if traspaso["para"] != agente:
            raise ErrorBoard("no_es_destinatario", f"Ese traspaso es para {traspaso['para']}.")
        if traspaso.get("acuse"):
            raise ErrorBoard("ya_confirmado", "Ese traspaso ya tiene acuse.")
        if encargo is None or encargo.get("id") != traspaso.get("encargo_id") or _traspaso_vigente(encargo) != traspaso_id:
            raise ErrorBoard("traspaso_no_vigente", "Ese traspaso ya no es el vigente del encargo: su acuse no cambiaría nada.")
        _verificar_rev(encargo, rev_esperada)
        acuse = {"id": _nuevo_id_acuse(ahora), "de": agente, "nota": nota, "registrado_en": _iso(ahora)}
        return _Plan(
            operacion={
                "tipo": "acuse",
                "id": acuse["id"],
                "traspaso_id": traspaso_id,
                "encargo_id": traspaso["encargo_id"],
                "version": traspaso["version"],
                "acuse": acuse,
                "hilo_ts": traspaso.get("slack_ts"),
            }
        )

    return await _escribir("registrar_acuse", agente, clave_idempotencia, argumentos, decidir, extras=(k_traspaso,))


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
