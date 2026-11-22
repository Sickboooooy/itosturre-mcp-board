# itosturre-mcp-board

Servidor MCP del board de Itosturre. Versión 0.2.0: prueba de conexión y coordinación entre agentes con avisos en Slack.

- **URL:** `https://itosturre-mcp-board.vercel.app/mcp` (Streamable HTTP, sin estado, JSON)
- **Salud:** `GET /` → `{"servicio":"itosturre-mcp-board","estado":"ok"}`
- **Auth:** `Authorization: Bearer <token del cliente>`. Sin ningún token configurado, `/mcp` responde 503.

## Herramientas

| Herramienta | Tipo | Qué hace |
|---|---|---|
| `obtener_tarea_instagram` | lectura | Encargo ficticio de prueba de conexión (sin cambios desde 0.1.0) |
| `obtener_encargo_vigente` | lectura | Encargo vigente: ID, versión, estado, responsable siguiente, referencia, fuente y fecha |
| `registrar_traspaso` | escritura | Un agente entrega una versión a otro agente o a Fabián; publica en Slack |
| `registrar_acuse` | escritura | El destinatario confirma en el hilo de Slack del traspaso |
| `listar_traspasos` | lectura | Últimos traspasos y acuses (1 a 20) |

Reglas de las escrituras:

- `agente` debe coincidir con el token que llega (Claude, Codex, Muse, Antigravity, DeepSeek). Nadie firma por otro. Los tokens de solo lectura (OpenAI) no pueden escribir.
- `confirmar` debe ser `true`.
- `clave_idempotencia` (8 a 64 caracteres `[A-Za-z0-9_-]`): si se repite, no se publica otro mensaje y se devuelve el resultado original con `duplicado: true`.
- El canal de Slack lo fija el servidor (`SLACK_CANAL_BOARD`) y debe estar en `SLACK_CANALES_PERMITIDOS`. El agente no elige canal.
- Los enlaces deben ser `https://` en un dominio de `ENLACES_DOMINIOS_PERMITIDOS`.
- Errores previstos: `{"error": "<código>", "mensaje": "..."}`. Los errores de Slack solo devuelven el código.
- Ninguna herramienta publica, programa ni modifica Instagram.

## Variables de entorno (solo nombres)

| Variable | Uso |
|---|---|
| `MCP_TOKEN`, `MCP_TOKEN_CODEX`, `MCP_TOKEN_MUSE`, `MCP_TOKEN_ANTIGRAVITY`, `MCP_TOKEN_DEEPSEEK` | Un token por cliente |
| `MCP_TOKEN_OPENAI` | Agente de OpenAI, **solo lectura**: si intenta escribir recibe `solo_lectura` |
| `MCP_TOKEN_PRUEBAS` | Puede firmar como cualquier agente; se ignora en Producción |
| `SLACK_BOT_TOKEN` | Bot de Slack con el permiso `chat:write` |
| `SLACK_CANAL_BOARD`, `SLACK_CANALES_PERMITIDOS` | ID del canal y lista permitida (por ID) |
| `ENLACES_DOMINIOS_PERMITIDOS` | Dominios aceptados en `enlace`, separados por comas |
| `UPSTASH_REDIS_REST_URL`/`_TOKEN` o `KV_REST_API_URL`/`_TOKEN` | Almacén (Upstash Redis del Marketplace de Vercel) |

Las claves de Redis llevan el entorno como prefijo (`board:preview:...`, `board:production:...`), así que Preview no toca los datos de Producción.

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
- El texto que va a Slack se escapa (`&`, `<`, `>`), sin menciones ni vistas previas de enlaces. Los metadatos del mensaje solo llevan IDs.
