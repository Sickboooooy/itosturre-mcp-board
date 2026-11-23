# itosturre-mcp-board

Servidor MCP del board de Itosturre. Versión 0.3.0: prueba de conexión y coordinación entre agentes con avisos en Slack, con un solo escritor, control de versión, idempotencia, vencimiento y recuperación (IT-005).

- **URL:** `https://itosturre-mcp-board.vercel.app/mcp` (Streamable HTTP, sin estado, JSON)
- **Salud:** `GET /` → `{"servicio":"itosturre-mcp-board","estado":"ok"}`
- **Auth:** `Authorization: Bearer <token del cliente>`. Sin ningún token configurado, `/mcp` responde 503 `servidor_sin_configurar`; si dos variables tienen el mismo token, 503 `configuracion_invalida`.

## Herramientas

| Herramienta | Tipo | Qué hace |
|---|---|---|
| `obtener_tarea_instagram` | lectura | Encargo ficticio de prueba de conexión (sin cambios desde 0.1.0) |
| `obtener_encargo_vigente` | lectura | Encargo vigente: ID, versión, `rev`, estado, responsable siguiente, arriendo (`arriendo_vencido`), referencia, fuente y fecha |
| `registrar_traspaso` | escritura | El responsable vigente (o Fabián) entrega la versión siguiente a otro agente o a Fabián; se anota y luego se publica en Slack |
| `registrar_acuse` | escritura | El destinatario del traspaso **vigente** confirma; se responde en el hilo de Slack |
| `renovar_arriendo` | escritura | El responsable extiende su arriendo (4 h desde ahora) antes de que venza. Sin Slack |
| `recuperar_encargo` | escritura | Con el arriendo del responsable vencido, quien llama toma el encargo. Sin Slack |
| `listar_traspasos` | lectura | Últimos traspasos y acuses (1 a 20), con el estado de su publicación |

Reglas de las escrituras:

- `agente` debe coincidir con el token que llega (Claude, Codex, Muse, Antigravity, DeepSeek; Fabián solo con `MCP_TOKEN_PROPIETARIO`). Nadie firma por otro. Los tokens de solo lectura (OpenAI) no pueden escribir.
- `confirmar` debe ser `true`.
- **Un escritor:** solo el `responsable_siguiente`, con su arriendo sin vencer, o Fabián pueden traspasar (`no_es_responsable`, `arriendo_vencido`).
- **Versión:** se entrega la siguiente (`v2` sobre `v1`). Repetir la vigente o saltar exige `justificacion_version`, que queda en el traspaso y en Slack (`version_repetida`, `version_salto`); retroceder nunca (`version_anterior`). Cada transición sube `rev`; `rev_esperada` (opcional) rechaza la escritura si el encargo cambió (`conflicto_revision`).
- **Atomicidad:** la decisión se toma sobre una foto del encargo y la escritura es un compare-and-set en Redis (script EVAL): si otro escribió en medio, se vuelve a leer y a decidir.
- **Acuse:** solo el del traspaso vigente (`traspaso_no_vigente`).
- **Slack después de anotar:** la operación queda en `operacion_en_curso` del encargo antes de publicar; mientras tanto, las demás escrituras reciben `operacion_en_curso`. Si Slack la rechaza, se descarta (y la clave queda libre). Si el resultado es incierto (`publicacion_incierta`), se reintenta con la misma clave y el board lo concilia buscando el mensaje por sus metadatos; no se publica dos veces.
- **`clave_idempotencia`** (8 a 64 caracteres `[A-Za-z0-9_-]`): ligada a la herramienta y a la huella de los argumentos. Si se repite con los mismos datos, no se publica otro mensaje y se devuelve el resultado original con `duplicado: true`; con otros datos u otra herramienta, `clave_reutilizada`.
- **Arriendo:** el destinatario de un traspaso recibe un arriendo de 4 h; el acuse lo reinicia y `renovar_arriendo` lo extiende. Vencido, cualquier agente con firma puede usar `recuperar_encargo`. Fabián no tiene arriendo: su revisión no se salta esperando.
- El canal de Slack lo fija el servidor (`SLACK_CANAL_BOARD`) y debe estar en `SLACK_CANALES_PERMITIDOS`. El agente no elige canal.
- Los enlaces deben ser `https://` en un dominio de `ENLACES_DOMINIOS_PERMITIDOS`.
- Errores previstos: `{"error": "<código>", "mensaje": "..."}`. Los errores de Slack solo devuelven el código.
- Ninguna herramienta publica, programa ni modifica Instagram.

## Variables de entorno (solo nombres)

| Variable | Uso |
|---|---|
| `MCP_TOKEN`, `MCP_TOKEN_CODEX`, `MCP_TOKEN_MUSE`, `MCP_TOKEN_ANTIGRAVITY`, `MCP_TOKEN_DEEPSEEK` | Un token por cliente; todos distintos |
| `MCP_TOKEN_OPENAI` | Agente de OpenAI, **solo lectura**: si intenta escribir recibe `solo_lectura` |
| `MCP_TOKEN_PROPIETARIO` | **Opcional (0.3.0).** Fabián: puede traspasar aunque no sea el responsable |
| `MCP_TOKEN_PRUEBAS` | Puede firmar como cualquier agente; **solo** se acepta con `VERCEL_ENV` exactamente `preview` o `development` |
| `SLACK_BOT_TOKEN` | Bot de Slack con `chat:write`; para conciliar publicaciones inciertas, también `channels:history` (canal público) o `groups:history` (privado) |
| `SLACK_CANAL_BOARD`, `SLACK_CANALES_PERMITIDOS` | ID del canal y lista permitida (por ID) |
| `ENLACES_DOMINIOS_PERMITIDOS` | Dominios aceptados en `enlace`, separados por comas |
| `UPSTASH_REDIS_REST_URL`/`_TOKEN` o `KV_REST_API_URL`/`_TOKEN` | Almacén (Upstash Redis del Marketplace de Vercel); debe admitir `EVAL` |

Las claves de Redis llevan el entorno como prefijo (`board:preview:...`, `board:production:...`), así que Preview no toca los datos de Producción.

## Migración a 0.3.0 (IT-005)

- **Herramientas:** las cinco anteriores conservan nombre y parámetros obligatorios. Se agregan `renovar_arriendo` y `recuperar_encargo`, y los parámetros opcionales `justificacion_version` y `rev_esperada`. `agente` admite `Fabián`.
- **Comportamiento que cambia:** traspasar exige ser el responsable (antes, cualquiera); repetir o saltar versión exige justificación (antes se aceptaba); el acuse de un traspaso que no es el vigente se rechaza (antes cambiaba el estado); reutilizar una clave con otros datos da `clave_reutilizada` (antes devolvía el resultado viejo); un resultado incierto de Slack da `publicacion_incierta` (antes `slack_no_disponible` y liberaba la clave).
- **Respuestas:** el encargo trae `rev`, `traspaso_vigente`, `arriendo` y, al leerlo, `arriendo_vencido`; los traspasos traen `publicacion` y, si aplica, `justificacion_version`; el acuse trae `id` y `publicacion`. Los metadatos del acuse en Slack agregan `id`.
- **Redis:** sin migración de datos. El encargo cargado a mano sigue funcionando (sin `rev` cuenta como 0; sin arriendo no vence). El valor de `idem:{agente}:{clave}` pasa de texto a JSON; una clave guardada antes de 0.3.0 responde `clave_reutilizada` (usar una nueva). Se dejan de usar `MULTI/EXEC` y se usa `EVAL`.
- **Vercel:** `MCP_TOKEN_PRUEBAS` deja de funcionar si `VERCEL_ENV` falta o no es `preview`/`development`. Si hay tokens repetidos, el servidor responde 503 hasta corregirlo. `MCP_TOKEN_PROPIETARIO` es nuevo y opcional.
- **Slack:** sin `channels:history`/`groups:history` el board sigue funcionando, pero una publicación incierta se resuelve al vencer (120 s) aplicándola sin volver a publicar (`publicacion.estado = no_confirmada`).

## Estructura

```
api/index.py             # servidor, autenticación y obtener_tarea_instagram
api/coordinacion.py      # herramientas de coordinación y validación
api/almacen.py           # estado en Upstash Redis
api/publicador_slack.py  # chat.postMessage y chat.getPermalink, con escapado
tests/                   # pruebas con Slack y Redis simulados
requirements.txt         # mcp, upstash-redis, httpx
.python-version          # 3.12
```

Vercel lo detecta como proyecto Python y enruta todas las peticiones a `app`. No uses `rewrites` en `vercel.json`: en proyectos de backend la app recibe la ruta reescrita y `/mcp` responde 404.

## Probar en local (WSL)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt uvicorn
python -m pytest -q tests                                     # Slack y Redis simulados
MCP_TOKEN="token-de-prueba" uvicorn --app-dir api index:app --port 8787
curl -s localhost:8787/                                     # 200
curl -s -o /dev/null -w "%{http_code}\n" -X POST localhost:8787/mcp -d '{}'   # 401
```

## Notas de diseño

- Se crea un gestor de sesión MCP **por petición** porque en serverless no está garantizado el evento `lifespan` de ASGI.
- La protección contra DNS rebinding del SDK está desactivada: solo aplica a servidores en localhost y aquí bloquearía el dominio público. El acceso lo controla el token.
- Los logs nunca registran tokens ni cabeceras.
- El texto que va a Slack se escapa (`&`, `<`, `>`), sin menciones ni vistas previas de enlaces. Los metadatos del mensaje solo llevan IDs; sirven también para conciliar una publicación incierta.
- El script de compare-and-set (`almacen.SCRIPT_CAS`) no se ejecuta en las pruebas locales (no hay Redis ni Lua): el doble lo emula. Conviene probarlo una vez contra el Upstash de Preview.
