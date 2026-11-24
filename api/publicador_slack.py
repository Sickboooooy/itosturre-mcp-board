"""Publicación en Slack para el board de Itosturre.

Reglas:
- Solo se publica en el canal configurado (SLACK_CANAL_BOARD) y solo si está en
  la lista permitida (SLACK_CANALES_PERMITIDOS). El agente nunca elige el canal.
- Sin vistas previas de enlaces y sin menciones: el texto se escapa antes de enviarlo.
- Los errores se reducen a un código (por ejemplo, not_in_channel). Nunca se
  propaga la respuesta cruda, la excepción original ni el token.
- Cada error dice si es incierto: si Slack pudo haber hecho la operación aunque no
  lo confirmó (timeout de lectura, 5xx, respuesta ilegible, internal_error). Los
  inciertos no se reintentan a ciegas: se concilian con buscar_evento (IT-005).
"""

from __future__ import annotations

import os
import re

import httpx

URL_API = "https://slack.com/api"
PATRON_CANAL = re.compile(r"^[CG][A-Z0-9]{8,12}$")
TIEMPO_ESPERA = httpx.Timeout(10.0)
# Fallas de transporte en las que la petición no llegó a Slack.
SIN_ENVIO = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
# Errores de la API en los que, según Slack, parte de la operación pudo haberse hecho.
ERRORES_INCIERTOS = frozenset({"internal_error", "fatal_error"})
PAGINAS_DE_BUSQUEDA = 5


class ErrorSlack(Exception):
    """Falla de Slack expresada solo con un código seguro de mostrar."""

    def __init__(self, codigo: str, incierto: bool = False):
        self.codigo = re.sub(r"[^a-z0-9_]", "", codigo.lower())[:60] or "desconocido"
        self.incierto = incierto
        super().__init__(self.codigo)


def escapar(texto: str) -> str:
    """Quita caracteres de control y escapa &, < y > según las reglas de Slack.

    Así una nota no puede generar <!channel>, <@usuario> ni enlaces ocultos.
    """
    limpio = "".join(c for c in texto if c in "\n\t" or (ord(c) >= 32 and ord(c) != 127))
    return limpio.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _lista_canales(valor: str) -> frozenset[str]:
    return frozenset(c.strip() for c in valor.split(",") if c.strip())


class PublicadorSlack:
    def __init__(self, token: str, canal: str, canales_permitidos: frozenset[str], transporte=None):
        self._token = token
        self.canal = canal
        self._permitidos = canales_permitidos
        self._transporte = transporte

    @classmethod
    def desde_entorno(cls) -> "PublicadorSlack":
        return cls(
            token=os.environ.get("SLACK_BOT_TOKEN", "").strip(),
            canal=os.environ.get("SLACK_CANAL_BOARD", "").strip(),
            canales_permitidos=_lista_canales(os.environ.get("SLACK_CANALES_PERMITIDOS", "")),
        )

    def verificar(self) -> None:
        """Comprueba la configuración antes de intentar publicar."""
        if not self._token:
            raise ErrorSlack("slack_no_configurado")
        if not PATRON_CANAL.match(self.canal):
            raise ErrorSlack("canal_no_configurado")
        if self.canal not in self._permitidos:
            raise ErrorSlack("canal_no_permitido")

    def _cliente(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=TIEMPO_ESPERA,
            transport=self._transporte,
            headers={"Authorization": f"Bearer {self._token}"},
        )

    async def _llamar(self, metodo_http: str, metodo_api: str, **kwargs) -> dict:
        try:
            async with self._cliente() as cliente:
                respuesta = await cliente.request(metodo_http, f"{URL_API}/{metodo_api}", **kwargs)
        except SIN_ENVIO:
            raise ErrorSlack("slack_no_disponible") from None
        except Exception:
            # No se reenvía el texto de la excepción: podría incluir cabeceras o URLs.
            # La petición pudo haber llegado (por ejemplo, timeout de lectura): incierto.
            raise ErrorSlack("slack_no_disponible", incierto=True) from None
        if respuesta.status_code == 429:
            raise ErrorSlack("ratelimited")
        if respuesta.status_code != 200:
            raise ErrorSlack(f"slack_http_{respuesta.status_code}", incierto=respuesta.status_code >= 500)
        try:
            datos = respuesta.json()
        except ValueError:
            raise ErrorSlack("respuesta_invalida", incierto=True) from None
        if not datos.get("ok"):
            error = str(datos.get("error", "desconocido"))
            raise ErrorSlack(error, incierto=error in ERRORES_INCIERTOS)
        return datos

    async def publicar(self, texto: str, metadatos: dict, hilo_ts: str | None = None) -> str:
        """Publica en el canal del board y devuelve el ts del mensaje."""
        self.verificar()
        cuerpo = {
            "channel": self.canal,
            "text": texto,
            "metadata": metadatos,
            "link_names": False,
            "unfurl_links": False,
            "unfurl_media": False,
        }
        if hilo_ts:
            cuerpo["thread_ts"] = hilo_ts
        datos = await self._llamar("POST", "chat.postMessage", json=cuerpo)
        ts = datos.get("ts")
        if not isinstance(ts, str):
            # Slack respondió ok pero sin ts: el mensaje existe y hay que conciliarlo.
            raise ErrorSlack("respuesta_invalida", incierto=True)
        return ts

    async def buscar_evento(self, tipo_evento: str, evento_id: str, hilo_ts: str | None = None, desde: float | None = None) -> str | None:
        """Busca en el canal un mensaje del board por sus metadatos y devuelve su ts, o None si no está.

        Con `hilo_ts` busca entre las respuestas de ese hilo (conversations.replies); si no, entre
        los mensajes del canal (conversations.history) desde `desde` (segundos Unix). Requiere el
        permiso de historial del canal (channels:history o groups:history). Si no puede revisar
        todo el intervalo, lanza ErrorSlack: «no encontrado» solo se afirma tras revisarlo completo.
        """
        self.verificar()
        params: dict = {"channel": self.canal, "include_all_metadata": "true", "limit": 200}
        if hilo_ts:
            metodo = "conversations.replies"
            params["ts"] = hilo_ts
        else:
            metodo = "conversations.history"
        if desde is not None:
            params["oldest"] = f"{desde:.6f}"
        for _ in range(PAGINAS_DE_BUSQUEDA):
            datos = await self._llamar("GET", metodo, params=params)
            for mensaje in datos.get("messages") or []:
                metadatos = mensaje.get("metadata") or {}
                carga = metadatos.get("event_payload") or {}
                if metadatos.get("event_type") == tipo_evento and carga.get("id") == evento_id and isinstance(mensaje.get("ts"), str):
                    return mensaje["ts"]
            cursor = (datos.get("response_metadata") or {}).get("next_cursor") or ""
            if not datos.get("has_more"):
                return None
            if not cursor:
                break
            params["cursor"] = cursor
        raise ErrorSlack("busqueda_incompleta")

    async def enlace_permanente(self, ts: str) -> str | None:
        """Permalink del mensaje. Si falla, devuelve None: no es crítico."""
        try:
            datos = await self._llamar("GET", "chat.getPermalink", params={"channel": self.canal, "message_ts": ts})
        except ErrorSlack:
            return None
        enlace = datos.get("permalink")
        return enlace if isinstance(enlace, str) else None
