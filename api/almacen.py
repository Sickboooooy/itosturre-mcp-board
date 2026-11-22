"""Estado del board en Upstash Redis (Marketplace de Vercel).

Claves, con el entorno como prefijo para que Preview no toque Producción:
  board:{entorno}:encargo:vigente        JSON del encargo vigente
  board:{entorno}:traspaso:{id}          JSON del traspaso (incluye su acuse)
  board:{entorno}:traspasos              lista de IDs, del más reciente al más antiguo
  board:{entorno}:idem:{agente}:{clave}  "en_proceso" o el resultado guardado

Cualquier falla se reduce a ErrorAlmacen("almacenamiento_no_disponible"), sin
detalles que puedan incluir credenciales.
"""

from __future__ import annotations

import json
import os

MAXIMO_TRASPASOS = 200
EN_PROCESO = "en_proceso"


class ErrorAlmacen(Exception):
    def __init__(self, codigo: str = "almacenamiento_no_disponible"):
        self.codigo = codigo
        super().__init__(codigo)


def _cargar(valor):
    if valor is None:
        return None
    if isinstance(valor, bytes):
        valor = valor.decode("utf-8")
    return json.loads(valor)


class AlmacenUpstash:
    def __init__(self, redis, entorno: str):
        self._r = redis
        self._p = f"board:{entorno}"

    @classmethod
    def desde_entorno(cls, entorno: str) -> "AlmacenUpstash":
        url = os.environ.get("UPSTASH_REDIS_REST_URL") or os.environ.get("KV_REST_API_URL")
        token = os.environ.get("UPSTASH_REDIS_REST_TOKEN") or os.environ.get("KV_REST_API_TOKEN")
        if not url or not token:
            raise ErrorAlmacen("almacenamiento_no_configurado")
        from upstash_redis.asyncio import Redis

        # Telemetría desactivada: no se envían datos de uso a terceros.
        return cls(Redis(url=url, token=token, allow_telemetry=False), entorno)

    # --- claves ---
    def _clave_encargo(self) -> str:
        return f"{self._p}:encargo:vigente"

    def _clave_traspaso(self, traspaso_id: str) -> str:
        return f"{self._p}:traspaso:{traspaso_id}"

    def _clave_lista(self) -> str:
        return f"{self._p}:traspasos"

    def _clave_idem(self, agente: str, clave: str) -> str:
        return f"{self._p}:idem:{agente}:{clave}"

    async def _seguro(self, corrutina):
        try:
            return await corrutina
        except ErrorAlmacen:
            raise
        except Exception:
            raise ErrorAlmacen() from None

    async def cerrar(self) -> None:
        try:
            await self._r.close()
        except Exception:
            pass

    # --- encargo y traspasos ---
    async def obtener_encargo(self) -> dict | None:
        return _cargar(await self._seguro(self._r.get(self._clave_encargo())))

    async def obtener_traspaso(self, traspaso_id: str) -> dict | None:
        return _cargar(await self._seguro(self._r.get(self._clave_traspaso(traspaso_id))))

    async def guardar_traspaso_y_encargo(self, traspaso: dict, encargo: dict | None, nuevo: bool) -> None:
        """Escribe el traspaso (y el encargo, si cambia) en una sola transacción."""

        async def _transaccion():
            t = self._r.multi()
            t.set(self._clave_traspaso(traspaso["id"]), json.dumps(traspaso, ensure_ascii=False))
            if nuevo:
                t.lpush(self._clave_lista(), traspaso["id"])
                t.ltrim(self._clave_lista(), 0, MAXIMO_TRASPASOS - 1)
            if encargo is not None:
                t.set(self._clave_encargo(), json.dumps(encargo, ensure_ascii=False))
            await t.exec()

        await self._seguro(_transaccion())

    async def listar_traspasos(self, limite: int) -> list[dict]:
        ids = await self._seguro(self._r.lrange(self._clave_lista(), 0, limite - 1))
        if not ids:
            return []
        valores = await self._seguro(self._r.mget(*[self._clave_traspaso(i) for i in ids]))
        return [t for t in (_cargar(v) for v in valores) if t]

    # --- idempotencia ---
    async def leer_idempotencia(self, agente: str, clave: str) -> str | None:
        valor = await self._seguro(self._r.get(self._clave_idem(agente, clave)))
        return valor.decode("utf-8") if isinstance(valor, bytes) else valor

    async def reservar_idempotencia(self, agente: str, clave: str, segundos: int) -> bool:
        resultado = await self._seguro(self._r.set(self._clave_idem(agente, clave), EN_PROCESO, nx=True, ex=segundos))
        return bool(resultado)

    async def guardar_idempotencia(self, agente: str, clave: str, valor: str, segundos: int) -> None:
        await self._seguro(self._r.set(self._clave_idem(agente, clave), valor, ex=segundos))

    async def liberar_idempotencia(self, agente: str, clave: str) -> None:
        try:
            await self._r.delete(self._clave_idem(agente, clave))
        except Exception:
            pass
