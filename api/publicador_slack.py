"""Publicación en Slack para el board de Itosturre.

Reglas:
- Solo se publica en el canal configurado (SLACK_CANAL_BOARD) y solo si está en
  la lista permitida (SLACK_CANALES_PERMITIDOS). El agente nunca elige el canal.
- Sin vistas previas de enlaces y sin menciones: el texto se escapa antes de enviarlo.
- Los errores se reducen a un código (por ejemplo, not_in_channel). Nunca se
  propaga la respuesta cruda, la excepción original ni el token.
"""

from __future__ import annotations

import os
import re

import httpx

URL_API = "https://slack.com/api"
PATRON_CANAL = re.compile(r"^[CG][A-Z0-9]{8,12}$")
TIEMPO_ESPERA = httpx.Timeout(10.0)


class ErrorSlack(Exception):
    """Falla de Slack expresada solo con un código seguro de mostrar."""

    def __init__(self, codigo: str):
        self.codigo = re.sub(r"[^a-z0-9_]", "", codigo.lower())[:60] or "desconocido"
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
        except Exception:
            # No se reenvía el texto de la excepción: podría incluir cabeceras o URLs.
            raise ErrorSlack("slack_no_disponible") from None
        if respuesta.status_code == 429:
            raise ErrorSlack("ratelimited")
        if respuesta.status_code != 200:
            raise ErrorSlack(f"slack_http_{respuesta.status_code}")
        try:
            datos = respuesta.json()
        except ValueError:
            raise ErrorSlack("respuesta_invalida") from None
        if not datos.get("ok"):
            raise ErrorSlack(str(datos.get("error", "desconocido")))
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
            raise ErrorSlack("respuesta_invalida")
        return ts

    async def enlace_permanente(self, ts: str) -> str | None:
        """Permalink del mensaje. Si falla, devuelve None: no es crítico."""
        try:
            datos = await self._llamar("GET", "chat.getPermalink", params={"channel": self.canal, "message_ts": ts})
        except ErrorSlack:
            return None
        enlace = datos.get("permalink")
        return enlace if isinstance(enlace, str) else None
