"""Estado del board en Upstash Redis (Marketplace de Vercel).

Claves, con el entorno como prefijo para que Preview no toque Producción:
  board:{entorno}:encargo:vigente        JSON del encargo vigente (con rev y, mientras se publica,
                                         la operación en curso)
  board:{entorno}:traspaso:{id}          JSON del traspaso (incluye su acuse y su publicación)
  board:{entorno}:traspasos              lista de IDs, del más reciente al más antiguo
  board:{entorno}:idem:{agente}:{clave}  JSON: operación, huella de los argumentos y estado
                                         (en_proceso con dueño y vencimiento, registrado o completado)

Lecturas y escrituras que deciden (IT-005):
- `leer(*claves)` toma una foto atómica (MGET) y devuelve, con cada valor, su «marca»: el
  texto exacto guardado (None si la clave no existe).
- `aplicar(condiciones, escrituras)` es un compare-and-set de varias claves en un solo script
  EVAL (SCRIPT_CAS): en el servidor comprueba que cada clave condicionada conserve la marca
  leída y solo entonces escribe todo. Si otro escribió entre la lectura y la escritura, no
  escribe nada y devuelve False. Las claves se nombran de forma lógica: ENCARGO,
  clave_traspaso(id), clave_idem(agente, clave); las escrituras son poner, borrar y
  apilar_traspaso.

Cualquier falla se reduce a ErrorAlmacen("almacenamiento_no_disponible"), sin
detalles que puedan incluir credenciales.
"""

from __future__ import annotations

import json
import os

MAXIMO_TRASPASOS = 200
# Valor que usaba la reserva de idempotencia antes de IT-005 (Redis lo borra solo a los 120 s).
EN_PROCESO = "en_proceso"
# IT005-R02: la lista de traspasos (el índice de listado) no es una lista, por ejemplo porque se
# editó a mano. SCRIPT_CAS no escribe nada y devuelve INDICE_DANADO_LUA; `aplicar` lo convierte en
# ErrorAlmacen(INDICE_DANADO) para que quien llama pueda guardar lo esencial sin el índice.
INDICE_DANADO = "indice_danado"
INDICE_DANADO_LUA = 2

# --- claves lógicas y escrituras ---
ENCARGO = ("encargo",)


def clave_traspaso(traspaso_id: str) -> tuple:
    return ("traspaso", traspaso_id)


def clave_idem(agente: str, clave: str) -> tuple:
    return ("idem", agente, clave)


def poner(clave: tuple, valor, segundos: int = 0) -> tuple:
    """Guarda `valor` como JSON; con `segundos` > 0, la clave caduca."""
    return ("poner", clave, valor, segundos)


def borrar(clave: tuple) -> tuple:
    return ("borrar", clave)


def apilar_traspaso(traspaso_id: str) -> tuple:
    """Agrega el ID al inicio de la lista de traspasos y la recorta a MAXIMO_TRASPASOS."""
    return ("apilar", traspaso_id)


def serializar(valor) -> str:
    """Texto que se guarda; es también la marca de lo que se escribe."""
    return json.dumps(valor, ensure_ascii=False)


# Compare-and-set de varias claves. Redis ejecuta el script completo sin intercalar otros
# comandos, así que la comprobación y las escrituras forman una sola operación.
#   ARGV[1]                  n: número de condiciones; KEYS[1..n] son las claves condicionadas.
#   ARGV[1+i] (i = 1..n)     "=" .. texto esperado, o "!" si la clave no debe existir.
#   después, grupos de 4     operación (SET, DEL o LPUSH), índice en KEYS, valor, número
#                            (SET: segundos de vida, 0 = sin caducidad; LPUSH: largo máximo).
# Devuelve:
#   1  escribió todo;
#   0  alguna condición no se cumplió: no escribió nada;
#   2  (INDICE_DANADO_LUA) un LPUSH apunta a una clave que no es una lista: no escribió nada;
#   error 'argumentos_invalidos': ARGV mal formado (un error del adaptador): no escribió nada.
# Todo o nada (IT005-R02): Redis no deshace las escrituras anteriores a un error dentro del script,
# así que nada que pueda fallar ocurre después de la primera escritura. Antes de escribir se validan
# los argumentos (números enteros canónicos, índices dentro de KEYS, operaciones conocidas), las
# condiciones y el tipo de cada clave donde se apila, teniendo en cuenta las escrituras anteriores
# del mismo script.
SCRIPT_CAS = """
local function entero(texto, minimo)
  if type(texto) ~= 'string' or #texto > 10 then return nil end
  if texto ~= '0' and not string.match(texto, '^[1-9]%d*$') then return nil end
  local valor = tonumber(texto)
  if valor < minimo then return nil end
  return valor
end
local n = entero(ARGV[1], 0)
if n == nil or n > #KEYS or #ARGV < 1 + n or (#ARGV - 1 - n) % 4 ~= 0 then
  return redis.error_reply('argumentos_invalidos')
end
for i = 1, n do
  local esperado = ARGV[1 + i]
  if esperado ~= '!' and string.sub(esperado, 1, 1) ~= '=' then
    return redis.error_reply('argumentos_invalidos')
  end
end
for j = n + 2, #ARGV, 4 do
  local op = ARGV[j]
  local indice = entero(ARGV[j + 1], 1)
  local minimo = 0
  if op == 'LPUSH' then minimo = 1 end
  if (op ~= 'SET' and op ~= 'DEL' and op ~= 'LPUSH') or indice == nil or indice > #KEYS or entero(ARGV[j + 3], minimo) == nil then
    return redis.error_reply('argumentos_invalidos')
  end
end
for i = 1, n do
  local esperado = ARGV[1 + i]
  local actual = redis.call('GET', KEYS[i])
  if esperado == '!' then
    if actual then return 0 end
  elseif actual ~= string.sub(esperado, 2) then
    return 0
  end
end
local tipos = {}
for j = n + 2, #ARGV, 4 do
  local op = ARGV[j]
  local clave = KEYS[tonumber(ARGV[j + 1])]
  if tipos[clave] == nil then
    local tipo = redis.call('TYPE', clave)
    if type(tipo) == 'table' then tipo = tipo['ok'] end
    tipos[clave] = tipo
  end
  if op == 'SET' then
    tipos[clave] = 'string'
  elseif op == 'DEL' then
    tipos[clave] = 'none'
  elseif tipos[clave] == 'list' or tipos[clave] == 'none' then
    tipos[clave] = 'list'
  else
    return 2
  end
end
for j = n + 2, #ARGV, 4 do
  local op = ARGV[j]
  local clave = KEYS[tonumber(ARGV[j + 1])]
  local numero = tonumber(ARGV[j + 3])
  if op == 'SET' then
    if numero > 0 then
      redis.call('SET', clave, ARGV[j + 2], 'EX', ARGV[j + 3])
    else
      redis.call('SET', clave, ARGV[j + 2])
    end
  elseif op == 'DEL' then
    redis.call('DEL', clave)
  else
    redis.call('LPUSH', clave, ARGV[j + 2])
    redis.call('LTRIM', clave, 0, numero - 1)
  end
end
return 1
"""


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

    def _fisica(self, clave: tuple) -> str:
        if clave == ENCARGO:
            return self._clave_encargo()
        if clave[0] == "traspaso":
            return self._clave_traspaso(clave[1])
        if clave[0] == "idem":
            return self._clave_idem(clave[1], clave[2])
        raise ValueError(f"clave desconocida: {clave[0]}")

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

    # --- foto atómica y compare-and-set ---
    def marca(self, valor) -> str | None:
        """Marca que tendrá `valor` una vez escrito con `poner`."""
        return None if valor is None else serializar(valor)

    async def leer(self, *claves: tuple) -> list[tuple]:
        """[(valor, marca)] de cada clave, leídos en un solo MGET.

        El valor es el JSON interpretado; si el texto no es JSON (datos anteriores a IT-005,
        como "en_proceso"), se devuelve el texto tal cual.
        """
        crudos = await self._seguro(self._r.mget(*[self._fisica(c) for c in claves]))
        resultado = []
        for crudo in crudos:
            if isinstance(crudo, bytes):
                crudo = crudo.decode("utf-8")
            if crudo is None:
                resultado.append((None, None))
                continue
            try:
                valor = json.loads(crudo)
            except ValueError:
                valor = crudo
            resultado.append((valor, crudo))
        return resultado

    async def aplicar(self, condiciones: list[tuple], escrituras: list[tuple]) -> bool:
        """Escribe todo solo si cada clave condicionada conserva su marca (None = no debe existir).

        Todo o nada: True si escribió todo; False si una condición no se cumplió. Si la lista de
        traspasos está dañada, no escribe nada y lanza ErrorAlmacen(INDICE_DANADO).
        """
        claves: list[str] = []
        indices: dict[str, int] = {}

        def indice(fisica: str) -> str:
            if fisica not in indices:
                claves.append(fisica)
                indices[fisica] = len(claves)
            return str(indices[fisica])

        argumentos = [str(len(condiciones))]
        for clave, marca in condiciones:
            fisica = self._fisica(clave)
            if fisica in indices:
                raise ValueError("cada clave se condiciona una sola vez")
            indice(fisica)
            argumentos.append("!" if marca is None else "=" + marca)
        for escritura in escrituras:
            if escritura[0] == "poner":
                _, clave, valor, segundos = escritura
                argumentos += ["SET", indice(self._fisica(clave)), serializar(valor), str(int(segundos or 0))]
            elif escritura[0] == "borrar":
                argumentos += ["DEL", indice(self._fisica(escritura[1])), "", "0"]
            elif escritura[0] == "apilar":
                argumentos += ["LPUSH", indice(self._clave_lista()), escritura[1], str(MAXIMO_TRASPASOS)]
            else:
                raise ValueError(f"escritura desconocida: {escritura[0]}")
        resultado = await self._seguro(self._r.eval(SCRIPT_CAS, keys=claves, args=argumentos))
        if resultado == INDICE_DANADO_LUA:
            raise ErrorAlmacen(INDICE_DANADO)
        return resultado == 1

    # --- encargo y traspasos ---
    async def obtener_encargo(self) -> dict | None:
        return _cargar(await self._seguro(self._r.get(self._clave_encargo())))

    async def obtener_traspaso(self, traspaso_id: str) -> dict | None:
        return _cargar(await self._seguro(self._r.get(self._clave_traspaso(traspaso_id))))

    async def listar_traspasos(self, limite: int) -> list[dict]:
        ids = await self._seguro(self._r.lrange(self._clave_lista(), 0, limite - 1))
        if not ids:
            return []
        valores = await self._seguro(self._r.mget(*[self._clave_traspaso(i) for i in ids]))
        return [t for t in (_cargar(v) for v in valores) if t]
