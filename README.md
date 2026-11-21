# itosturre-mcp-board

Servidor MCP del board de Itosturre. Versión 0.1.0: prueba de conexión, solo lectura.

- **URL:** `https://itosturre-mcp-board.vercel.app/mcp` (Streamable HTTP, sin estado, JSON)
- **Salud:** `GET /` → `{"servicio":"itosturre-mcp-board","estado":"ok"}`
- **Herramienta:** `obtener_tarea_instagram`, que devuelve un encargo ficticio.
- **Auth:** `Authorization: Bearer <MCP_TOKEN>`. Si `MCP_TOKEN` no está definida, `/mcp` responde 503.

## Estructura

```
api/index.py       # servidor (SDK oficial mcp 2.3.0 + Starlette)
requirements.txt   # mcp==2.3.0
.python-version    # 3.12
```

Vercel lo detecta como proyecto Python y enruta todas las peticiones a `app`. No uses `rewrites` en `vercel.json`: en proyectos de backend la app recibe la ruta reescrita y `/mcp` responde 404.

## Probar en local (WSL)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt uvicorn
MCP_TOKEN="token-de-prueba" uvicorn --app-dir api index:app --port 8787
curl -s localhost:8787/                                     # 200
curl -s -o /dev/null -w "%{http_code}\n" -X POST localhost:8787/mcp -d '{}'   # 401
```

## Notas de diseño

- Se crea un gestor de sesión MCP **por petición** porque en serverless no está garantizado el evento `lifespan` de ASGI.
- La protección contra DNS rebinding del SDK está desactivada: solo aplica a servidores en localhost y aquí bloquearía el dominio público. El acceso lo controla el token.
- Los logs nunca registran tokens ni cabeceras.
