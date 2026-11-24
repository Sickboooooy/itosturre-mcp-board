"""IT-005 · IT005-R03: una justificación vacía de contenido no permite repetir ni saltar versión.

`justificacion_version` se normaliza: se quitan los caracteres de control y de formato (invisibles,
como U+200B) y los espacios y saltos de línea sobrantes. Si no queda nada visible, cuenta como
ausente. El encargo inicial de conftest está en v1 con responsable Codex.

Los caracteres invisibles se escriben con su nombre (\\N{...}) para que se lean en el código.
"""

from __future__ import annotations

import pytest
from conftest import TOKENS, llamar

import coordinacion

pytestmark = pytest.mark.anyio

CODEX = TOKENS["MCP_TOKEN_CODEX"]
CLAUDE = TOKENS["MCP_TOKEN"]

ANCHO_CERO = "\N{ZERO WIDTH SPACE}"  # U+200B
VACIAS = {
    "espacio": " ",
    "tabulador_y_salto": "\t\n",
    "ancho_cero": ANCHO_CERO,
    "controles": "\r\n \x00\x07\x1b\x7f",
    "espacios_unicode": "\N{NO-BREAK SPACE}\N{EM SPACE}\N{IDEOGRAPHIC SPACE}\N{LINE SEPARATOR}\N{PARAGRAPH SEPARATOR}",
    "formato_invisible": (
        "\N{ZERO WIDTH NO-BREAK SPACE}\N{WORD JOINER}\N{ZERO WIDTH NON-JOINER}"
        "\N{ZERO WIDTH JOINER}\N{SOFT HYPHEN}\N{RIGHT-TO-LEFT OVERRIDE}"
    ),
    "rellenos_invisibles": "\N{HANGUL FILLER}\N{HANGUL CHOSEONG FILLER}\N{BRAILLE PATTERN BLANK}",
    "mezcla": f" {ANCHO_CERO}\t\N{IDEOGRAPHIC SPACE}\n\N{ZERO WIDTH NO-BREAK SPACE} ",
}


def traspaso(**cambios) -> dict:
    base = {
        "agente": "Codex",
        "encargo_id": "IG-01",
        "version": "v2",
        "entrega_a": "Claude",
        "enlace": "https://docs.google.com/document/d/ficticio-v2",
        "nota": "Integré el guion ficticio.",
        "clave_idempotencia": "codex-ig01-v2-0001",
        "confirmar": True,
    }
    base.update(cambios)
    return base


@pytest.mark.parametrize("justificacion", list(VACIAS.values()), ids=list(VACIAS))
def test_justificacion_vacia_no_permite_saltar_version(justificacion):
    """La reproducción de Hermes: _verificar_version({"version": "v1"}, "v9", justificación)."""
    with pytest.raises(coordinacion.ErrorBoard) as error:
        coordinacion._verificar_version({"version": "v1"}, "v9", justificacion)
    assert error.value.codigo == "version_salto"


@pytest.mark.parametrize("justificacion", list(VACIAS.values()), ids=list(VACIAS))
def test_justificacion_vacia_no_permite_repetir_version(justificacion):
    with pytest.raises(coordinacion.ErrorBoard) as error:
        coordinacion._verificar_version({"version": "v1"}, "v1", justificacion)
    assert error.value.codigo == "version_repetida"


async def test_traspaso_con_justificacion_de_espacios_no_salta_version(cliente_http, board):
    """El escenario de Codex: version v9 sobre v1 con justificacion_version=" "."""
    almacen, slack = board
    _, es_error, datos = await llamar(cliente_http, CODEX, "registrar_traspaso", traspaso(version="v9", justificacion_version=" "))
    assert es_error and datos["error"] == "version_salto", datos
    assert slack.peticiones == []
    assert almacen.encargo["version"] == "v1"


async def test_traspaso_con_justificacion_invisible_no_repite_version(cliente_http, board):
    almacen, slack = board
    _, es_error, _ = await llamar(cliente_http, CODEX, "registrar_traspaso", traspaso())
    assert not es_error
    _, es_error, datos = await llamar(
        cliente_http,
        CLAUDE,
        "registrar_traspaso",
        traspaso(agente="Claude", entrega_a="Muse", clave_idempotencia="claude-ig01-v2-0001", justificacion_version=f"{ANCHO_CERO}\t\n"),
    )
    assert es_error and datos["error"] == "version_repetida", datos
    assert len(slack.publicados) == 1
    assert almacen.encargo["responsable_siguiente"] == "Claude"


async def test_justificacion_vacia_no_se_guarda_ni_se_publica(cliente_http, board):
    """En la versión siguiente no hace falta justificación: una vacía cuenta como ausente, así que ni
    queda en el traspaso ni aparece en Slack."""
    almacen, slack = board
    _, es_error, datos = await llamar(cliente_http, CODEX, "registrar_traspaso", traspaso(justificacion_version=f" {ANCHO_CERO}\n"))
    assert not es_error, datos
    assert "justificacion_version" not in almacen.traspasos[datos["traspaso_id"]]
    assert "Justificación de versión" not in slack.publicados[0]["text"]


async def test_justificacion_valida_se_guarda_normalizada(cliente_http, board):
    almacen, slack = board
    _, es_error, datos = await llamar(
        cliente_http,
        CODEX,
        "registrar_traspaso",
        traspaso(version="v4", justificacion_version=f"  {ANCHO_CERO}Se descartaron v2\ny v3{ANCHO_CERO} en la revisión.\t "),
    )
    assert not es_error, datos
    assert almacen.traspasos[datos["traspaso_id"]]["justificacion_version"] == "Se descartaron v2 y v3 en la revisión."
    assert "Justificación de versión: Se descartaron v2 y v3 en la revisión.\n" in slack.publicados[0]["text"]
    assert almacen.encargo["version"] == "v4"
