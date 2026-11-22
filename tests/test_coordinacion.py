"""Pruebas del servidor MCP del board: autenticación, validación, Slack e idempotencia.

Slack y Redis están simulados (ver conftest.py); nada sale a la red.
"""

from __future__ import annotations

import logging

import pytest
from conftest import CANAL_PRUEBAS, TOKEN_SLACK, TOKENS, llamar

pytestmark = pytest.mark.anyio

CLAUDE = TOKENS["MCP_TOKEN"]
CODEX = TOKENS["MCP_TOKEN_CODEX"]
MUSE = TOKENS["MCP_TOKEN_MUSE"]
PRUEBAS = TOKENS["MCP_TOKEN_PRUEBAS"]
OPENAI = TOKENS["MCP_TOKEN_OPENAI"]


def traspaso(**cambios) -> dict:
    base = {
        "agente": "Codex",
        "encargo_id": "IG-01",
        "version": "v2",
        "entrega_a": "Claude",
        "enlace": "https://docs.google.com/document/d/ficticio-v2",
        "nota": "Integré el guion ficticio. Falta revisión de ritmo.",
        "clave_idempotencia": "codex-ig01-v2-0001",
        "confirmar": True,
    }
    base.update(cambios)
    return base


# --- autenticación y lo que no debe cambiar ---
async def test_sin_token_responde_401(cliente_http, entorno):
    estado, _, cuerpo = await llamar(cliente_http, None, "obtener_encargo_vigente")
    assert estado == 401
    assert "no_autorizado" in cuerpo


async def test_token_incorrecto_responde_401(cliente_http, entorno):
    estado, _, _ = await llamar(cliente_http, "token-falso-000000000000000000000000000000", "listar_traspasos")
    assert estado == 401


async def test_salud_sigue_igual(cliente_http, entorno):
    respuesta = await cliente_http.get("/")
    assert respuesta.status_code == 200
    assert respuesta.json()["estado"] == "ok"


async def test_obtener_tarea_instagram_sin_cambios(cliente_http, entorno):
    estado, es_error, datos = await llamar(cliente_http, CLAUDE, "obtener_tarea_instagram")
    assert estado == 200 and not es_error
    assert datos["id"] == "TAREA-PRUEBA-001"
    assert datos["ficticia"] is True
    assert set(datos) == {"id", "ficticia", "titulo", "encargo", "rol_sugerido", "restricciones", "origen", "generado_en"}


async def test_lista_de_herramientas(cliente_http, entorno):
    respuesta = await cliente_http.post(
        "/mcp",
        headers={"authorization": f"Bearer {CODEX}", "accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    nombres = {h["name"] for h in respuesta.json()["result"]["tools"]}
    assert nombres == {
        "obtener_tarea_instagram",
        "obtener_encargo_vigente",
        "registrar_traspaso",
        "registrar_acuse",
        "listar_traspasos",
    }


async def test_token_de_pruebas_rechazado_en_produccion(cliente_http, board, monkeypatch):
    monkeypatch.setenv("VERCEL_ENV", "production")
    estado, _, _ = await llamar(cliente_http, PRUEBAS, "obtener_encargo_vigente")
    assert estado == 401


async def test_token_de_pruebas_firma_como_cualquiera_en_preview(cliente_http, board):
    _, slack = board
    estado, es_error, datos = await llamar(cliente_http, PRUEBAS, "registrar_traspaso", traspaso(agente="Muse", entrega_a="Codex"))
    assert estado == 200 and not es_error, datos
    assert len(slack.mensajes) == 1


# --- cliente de solo lectura (agente de OpenAI) ---
async def test_openai_puede_leer(cliente_http, board):
    _, es_error, encargo = await llamar(cliente_http, OPENAI, "obtener_encargo_vigente")
    assert not es_error and encargo["id"] == "IG-01"
    _, es_error, lista = await llamar(cliente_http, OPENAI, "listar_traspasos")
    assert not es_error and lista == {"traspasos": []}
    _, es_error, tarea = await llamar(cliente_http, OPENAI, "obtener_tarea_instagram")
    assert not es_error and tarea["id"] == "TAREA-PRUEBA-001"


@pytest.mark.parametrize("agente", ["Codex", "Claude", "Muse", "Antigravity", "DeepSeek"])
async def test_openai_no_puede_registrar_traspasos(cliente_http, board, agente):
    almacen, slack = board
    _, es_error, datos = await llamar(cliente_http, OPENAI, "registrar_traspaso", traspaso(agente=agente, entrega_a="Fabián"))
    assert es_error and datos["error"] == "solo_lectura"
    assert slack.peticiones == []
    assert almacen.encargo["version"] == "v1"


async def test_openai_no_puede_registrar_acuses(cliente_http, board):
    _, slack = board
    _, _, t = await llamar(cliente_http, CODEX, "registrar_traspaso", traspaso())
    _, es_error, datos = await llamar(
        cliente_http,
        OPENAI,
        "registrar_acuse",
        {"agente": "Claude", "traspaso_id": t["traspaso_id"], "clave_idempotencia": "openai-acuse-0001", "confirmar": True},
    )
    assert es_error and datos["error"] == "solo_lectura"
    assert len(slack.mensajes) == 1


# --- lectura ---
async def test_obtener_encargo_vigente(cliente_http, board):
    _, es_error, datos = await llamar(cliente_http, MUSE, "obtener_encargo_vigente")
    assert not es_error
    assert datos["id"] == "IG-01" and datos["version"] == "v1"
    assert datos["fuente"]["tipo"] == "carga_inicial"


async def test_sin_encargo_vigente(cliente_http, board):
    almacen, _ = board
    almacen.encargo = None
    _, es_error, datos = await llamar(cliente_http, MUSE, "obtener_encargo_vigente")
    assert es_error and datos["error"] == "sin_encargo_vigente"


# --- traspasos ---
async def test_traspaso_publica_y_actualiza_encargo(cliente_http, board):
    almacen, slack = board
    _, es_error, datos = await llamar(cliente_http, CODEX, "registrar_traspaso", traspaso())
    assert not es_error, datos
    assert datos["duplicado"] is False
    assert datos["traspaso_id"].startswith("T-")
    assert datos["permalink"].startswith(f"https://itosturre.slack.com/archives/{CANAL_PRUEBAS}/")
    assert datos["encargo"]["version"] == "v2"
    assert datos["encargo"]["estado"] == "pendiente_acuse"
    assert datos["encargo"]["responsable_siguiente"] == "Claude"
    assert datos["encargo"]["fuente"]["traspaso_id"] == datos["traspaso_id"]
    assert datos["encargo"]["nota"] == traspaso()["nota"]

    mensaje = slack.mensajes[0]
    assert mensaje["channel"] == CANAL_PRUEBAS
    assert mensaje["link_names"] is False and mensaje["unfurl_links"] is False and mensaje["unfurl_media"] is False
    assert mensaje["metadata"]["event_type"] == "traspaso_registrado"
    assert set(mensaje["metadata"]["event_payload"]) == {"id", "encargo_id", "version", "de", "para"}
    assert almacen.encargo["version"] == "v2"


async def test_traspaso_a_fabian_queda_en_revision(cliente_http, board):
    _, _, datos = await llamar(cliente_http, CODEX, "registrar_traspaso", traspaso(entrega_a="Fabián"))
    assert datos["encargo"]["estado"] == "en_revision_fabian"


async def test_agente_no_coincide_con_token(cliente_http, board):
    _, slack = board
    _, es_error, datos = await llamar(cliente_http, CODEX, "registrar_traspaso", traspaso(agente="Claude", entrega_a="Muse"))
    assert es_error and datos["error"] == "agente_no_coincide"
    assert slack.peticiones == []


async def test_deepseek_sin_token_propio_no_puede_firmar(cliente_http, board):
    _, es_error, datos = await llamar(cliente_http, CODEX, "registrar_traspaso", traspaso(agente="DeepSeek"))
    assert es_error and datos["error"] == "agente_no_coincide"


async def test_confirmar_falso_no_escribe(cliente_http, board):
    _, slack = board
    _, es_error, datos = await llamar(cliente_http, CODEX, "registrar_traspaso", traspaso(confirmar=False))
    assert es_error and datos["error"] == "confirmacion_requerida"
    assert slack.peticiones == []


async def test_entregarse_a_si_mismo(cliente_http, board):
    _, es_error, datos = await llamar(cliente_http, CODEX, "registrar_traspaso", traspaso(entrega_a="Codex"))
    assert es_error and datos["error"] == "entrega_a_si_mismo"


@pytest.mark.parametrize(
    "cambios",
    [
        {"encargo_id": "ig-01"},
        {"version": "2"},
        {"entrega_a": "Desconocido"},
        {"agente": "Gemini"},
        {"nota": ""},
        {"nota": "x" * 501},
        {"clave_idempotencia": "corta"},
        {"clave_idempotencia": "con espacios 123"},
        {"enlace": "https://" + "a" * 600},
    ],
)
async def test_validacion_de_entradas(cliente_http, board, cambios):
    _, slack = board
    _, es_error, _ = await llamar(cliente_http, CODEX, "registrar_traspaso", traspaso(**cambios))
    assert es_error
    assert slack.peticiones == []


@pytest.mark.parametrize(
    "enlace, codigo",
    [
        ("http://docs.google.com/document/d/x", "enlace_invalido"),
        ("https://evil.example.com/x", "dominio_no_permitido"),
        ("https://docs.google.com.evil.com/x", "dominio_no_permitido"),
        ("https://usuario:clave@docs.google.com/x", "enlace_invalido"),
        ("https://docs.google.com:8443/x", "enlace_invalido"),
        ("https://docs.google.com/x|<!channel>", "enlace_invalido"),
        ("javascript:alert(1)//docs.google.com", "enlace_invalido"),
    ],
)
async def test_enlaces_rechazados(cliente_http, board, enlace, codigo):
    _, slack = board
    _, es_error, datos = await llamar(cliente_http, CODEX, "registrar_traspaso", traspaso(enlace=enlace))
    assert es_error and datos["error"] == codigo
    assert slack.peticiones == []


async def test_dominios_configurables(cliente_http, board, monkeypatch):
    monkeypatch.setenv("ENLACES_DOMINIOS_PERMITIDOS", "github.com")
    _, es_error, datos = await llamar(cliente_http, CODEX, "registrar_traspaso", traspaso())
    assert es_error and datos["error"] == "dominio_no_permitido"


async def test_encargo_distinto(cliente_http, board):
    _, es_error, datos = await llamar(cliente_http, CODEX, "registrar_traspaso", traspaso(encargo_id="IG-02"))
    assert es_error and datos["error"] == "encargo_distinto"


async def test_version_anterior(cliente_http, board):
    almacen, _ = board
    almacen.encargo["version"] = "v3"
    _, es_error, datos = await llamar(cliente_http, CODEX, "registrar_traspaso", traspaso(version="v2"))
    assert es_error and datos["error"] == "version_anterior"


async def test_escapado_de_menciones_y_html(cliente_http, board):
    _, slack = board
    await llamar(cliente_http, CODEX, "registrar_traspaso", traspaso(nota="Ojo <!channel> & <@U123> <https://x.com|clic>"))
    texto = slack.mensajes[0]["text"]
    assert "<!channel>" not in texto and "<@U123>" not in texto
    assert "&lt;!channel&gt; &amp; &lt;@U123&gt;" in texto


# --- idempotencia ---
async def test_idempotencia_un_solo_mensaje(cliente_http, board):
    _, slack = board
    _, _, primero = await llamar(cliente_http, CODEX, "registrar_traspaso", traspaso())
    _, es_error, segundo = await llamar(cliente_http, CODEX, "registrar_traspaso", traspaso())
    assert not es_error
    assert segundo["duplicado"] is True
    assert segundo["traspaso_id"] == primero["traspaso_id"]
    assert len(slack.mensajes) == 1


async def test_misma_clave_distinto_agente_no_choca(cliente_http, board):
    _, slack = board
    await llamar(cliente_http, CODEX, "registrar_traspaso", traspaso())
    _, es_error, datos = await llamar(
        cliente_http, CLAUDE, "registrar_traspaso", traspaso(agente="Claude", entrega_a="Fabián", version="v2")
    )
    assert not es_error, datos
    assert len(slack.mensajes) == 2


async def test_registro_incompleto_no_duplica_al_reintentar(cliente_http, board):
    almacen, slack = board
    almacen.fallar_al_guardar = True
    _, es_error, datos = await llamar(cliente_http, CODEX, "registrar_traspaso", traspaso())
    assert es_error and datos["error"] == "registro_incompleto"
    _, es_error, datos = await llamar(cliente_http, CODEX, "registrar_traspaso", traspaso())
    assert es_error and datos["error"] == "registro_incompleto"
    assert len(slack.mensajes) == 1


# --- Slack: canal permitido y errores sin secretos ---
async def test_canal_no_permitido(cliente_http, board, monkeypatch):
    _, slack = board
    monkeypatch.setenv("SLACK_CANALES_PERMITIDOS", "C0965E5KYNN")
    _, es_error, datos = await llamar(cliente_http, CODEX, "registrar_traspaso", traspaso())
    assert es_error and datos["error"] == "canal_no_permitido"
    assert slack.peticiones == []


async def test_sin_token_de_slack(cliente_http, board, monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN")
    _, es_error, datos = await llamar(cliente_http, CODEX, "registrar_traspaso", traspaso())
    assert es_error and datos["error"] == "slack_no_configurado"


async def test_error_de_slack_sin_filtrar_secretos_y_libera_la_clave(cliente_http, board, caplog):
    almacen, slack = board
    slack.modo = "error"
    caplog.set_level(logging.DEBUG)
    estado, es_error, datos = await llamar(cliente_http, CODEX, "registrar_traspaso", traspaso())
    assert estado == 200 and es_error and datos["error"] == "not_in_channel"
    assert TOKEN_SLACK not in str(datos) and TOKEN_SLACK not in caplog.text
    assert almacen.encargo["version"] == "v1"
    # Corregido el problema, la misma clave puede reintentarse.
    slack.modo = "ok"
    _, es_error, datos = await llamar(cliente_http, CODEX, "registrar_traspaso", traspaso())
    assert not es_error and datos["duplicado"] is False


async def test_falla_de_red_de_slack_sin_filtrar_secretos(cliente_http, board, caplog):
    _, slack = board
    slack.modo = "excepcion"
    caplog.set_level(logging.DEBUG)
    _, es_error, datos = await llamar(cliente_http, CODEX, "registrar_traspaso", traspaso())
    assert es_error and datos["error"] == "slack_no_disponible"
    assert TOKEN_SLACK not in str(datos) and TOKEN_SLACK not in caplog.text


def test_httpx_no_registra_urls():
    import index  # noqa: F401  (aplica la configuración de logs)

    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


async def test_logs_de_rechazo_sin_tokens(cliente_http, entorno, caplog):
    caplog.set_level(logging.DEBUG)
    falso = "token-intruso-con-valor-secreto-000000000000"
    await llamar(cliente_http, falso, "listar_traspasos")
    assert falso not in caplog.text
    assert all(valor not in caplog.text for valor in TOKENS.values())


# --- acuses ---
async def test_acuse_en_hilo_y_encargo_en_curso(cliente_http, board):
    almacen, slack = board
    _, _, t = await llamar(cliente_http, CODEX, "registrar_traspaso", traspaso())
    _, es_error, datos = await llamar(
        cliente_http,
        CLAUDE,
        "registrar_acuse",
        {"agente": "Claude", "traspaso_id": t["traspaso_id"], "clave_idempotencia": "claude-acuse-0001", "confirmar": True, "nota": "Recibido."},
    )
    assert not es_error, datos
    assert datos["permalink_respuesta"]
    acuse = slack.mensajes[1]
    assert acuse["thread_ts"] == slack.mensajes[0].get("thread_ts", "1795370000.000001")
    assert acuse["metadata"]["event_type"] == "acuse_registrado"
    assert almacen.encargo["estado"] == "en_curso"
    assert almacen.traspasos[t["traspaso_id"]]["acuse"]["de"] == "Claude"


async def test_acuse_solo_del_destinatario(cliente_http, board):
    _, slack = board
    _, _, t = await llamar(cliente_http, CODEX, "registrar_traspaso", traspaso())
    _, es_error, datos = await llamar(
        cliente_http,
        MUSE,
        "registrar_acuse",
        {"agente": "Muse", "traspaso_id": t["traspaso_id"], "clave_idempotencia": "muse-acuse-0001", "confirmar": True},
    )
    assert es_error and datos["error"] == "no_es_destinatario"
    assert len(slack.mensajes) == 1


async def test_acuse_duplicado_y_ya_confirmado(cliente_http, board):
    _, slack = board
    _, _, t = await llamar(cliente_http, CODEX, "registrar_traspaso", traspaso())
    args = {"agente": "Claude", "traspaso_id": t["traspaso_id"], "clave_idempotencia": "claude-acuse-0001", "confirmar": True}
    await llamar(cliente_http, CLAUDE, "registrar_acuse", args)
    _, es_error, repetido = await llamar(cliente_http, CLAUDE, "registrar_acuse", args)
    assert not es_error and repetido["duplicado"] is True
    _, es_error, datos = await llamar(cliente_http, CLAUDE, "registrar_acuse", {**args, "clave_idempotencia": "claude-acuse-0002"})
    assert es_error and datos["error"] == "ya_confirmado"
    assert len(slack.mensajes) == 2


async def test_acuse_de_traspaso_inexistente(cliente_http, board):
    _, es_error, datos = await llamar(
        cliente_http,
        CLAUDE,
        "registrar_acuse",
        {"agente": "Claude", "traspaso_id": "T-20261122-000000-abcd", "clave_idempotencia": "claude-acuse-0003", "confirmar": True},
    )
    assert es_error and datos["error"] == "traspaso_no_encontrado"


# --- listado ---
async def test_listar_traspasos_del_mas_reciente(cliente_http, board):
    _, _, a = await llamar(cliente_http, CODEX, "registrar_traspaso", traspaso())
    _, _, b = await llamar(
        cliente_http, CLAUDE, "registrar_traspaso", traspaso(agente="Claude", entrega_a="Fabián", version="v3", clave_idempotencia="claude-ig01-v3")
    )
    _, es_error, datos = await llamar(cliente_http, MUSE, "listar_traspasos", {"n": 5})
    assert not es_error
    assert [t["id"] for t in datos["traspasos"]] == [b["traspaso_id"], a["traspaso_id"]]
    _, _, filtrado = await llamar(cliente_http, MUSE, "listar_traspasos", {"encargo_id": "IG-99"})
    assert filtrado["traspasos"] == []


async def test_listar_fuera_de_rango(cliente_http, board):
    _, es_error, _ = await llamar(cliente_http, MUSE, "listar_traspasos", {"n": 21})
    assert es_error
