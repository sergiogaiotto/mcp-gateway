"""Peças visuais comuns: cabeçalho, selos, tabelas simples e caixas de erro.

Todo texto vindo de dados (títulos, descrições, mensagens) entra por
``ui.label``, que escapa o conteúdo; nada aqui monta HTML com dados.
"""

from collections.abc import Awaitable, Callable, Sequence

from nicegui import ui

from rapa.core.contracts import ContractError, RunState
from rapa.core.robot import Effect
from rapa.ui.auth import LOGIN_PATH
from rapa.ui.backend import BackendError, NotAuthenticated, describe_error
from rapa.ui.context import Viewer
from rapa.ui.labels import EFFECT_COLORS, EFFECT_LABELS, RUN_STATE_COLORS, RUN_STATE_LABELS
from rapa.ui.permissions import can_record

__all__ = [
    "NAV_ITEMS",
    "effect_badge",
    "error_box",
    "notify_error",
    "page_frame",
    "run_action",
    "simple_table",
    "state_badge",
]

NAV_ITEMS: tuple[tuple[str, str, str], ...] = (
    ("catalogo", "Catálogo", "/catalogo"),
    ("robos", "Robôs", "/robos"),
    ("execucoes", "Execuções", "/execucoes"),
    ("gravar", "Gravar", "/gravar"),
)
#: Itens que só aparecem para quem pode usá-los (o servidor confere de novo).
_NAV_RULES = {"gravar": can_record}


def page_frame(viewer: Viewer, active: str) -> ui.column:
    """Cabeçalho com navegação e quem está logado; devolve o contêiner principal."""
    settings = viewer.ctx.settings
    with ui.header().classes("items-center justify-between bg-blue-grey-9"):
        with ui.row().classes("items-center gap-4"):
            ui.label(settings.title).classes("text-h6 text-white")
            for key, label, path in NAV_ITEMS:
                rule = _NAV_RULES.get(key)
                if rule is not None and not rule(viewer.principal):
                    continue
                link = ui.link(label, path).classes("text-white no-underline").mark(f"nav-{key}")
                if key == active:
                    link.classes("text-weight-bold underline")
        with ui.row().classes("items-center gap-2"):
            ui.label(viewer.display_name).classes("text-white").mark("usuario-logado")
            ui.label(settings.company_label(viewer.principal.company_id)).classes(
                "text-white text-caption"
            ).mark("empresa-logada")
            ui.button("Sair", on_click=lambda: ui.navigate.to("/auth/sair")).props(
                "flat color=white"
            ).mark("sair")
    return ui.column().classes("w-full max-w-6xl mx-auto p-4 gap-4")


def effect_badge(effect: Effect) -> ui.badge:
    return ui.badge(EFFECT_LABELS[effect], color=EFFECT_COLORS[effect]).mark(f"efeito-{effect}")


def state_badge(state: RunState) -> ui.badge:
    return ui.badge(RUN_STATE_LABELS[state], color=RUN_STATE_COLORS[state]).mark("situacao")


def simple_table(
    headers: Sequence[str], rows: Sequence[Sequence[str]], *, marker: str | None = None
) -> ui.grid:
    """Tabela só de texto, em grade (cada célula é um ``ui.label``)."""
    grid = ui.grid(columns=len(headers)).classes("w-full gap-x-4 gap-y-1")
    if marker:
        grid.mark(marker)
    with grid:
        for header in headers:
            ui.label(header).classes("text-weight-bold")
        for row in rows:
            for cell in row:
                ui.label(cell).classes("break-words")
    return grid


def error_box(exc: ContractError | BackendError) -> None:
    message, details = describe_error(exc)
    with ui.card().classes("w-full bg-red-1").mark("erro"):
        ui.label(message).classes("text-negative text-weight-medium")
        for detail in details:
            ui.label(detail).classes("text-negative text-caption")


def notify_error(exc: ContractError | BackendError) -> None:
    message, details = describe_error(exc)
    text = message if not details else f"{message}: {'; '.join(details[:5])}"
    ui.notify(text, type="negative", multi_line=True)


async def run_action(action: Callable[[], Awaitable[None]]) -> None:
    """Executa uma ação de botão mostrando os erros em pt-BR."""
    try:
        await action()
    except NotAuthenticated:
        ui.notify("Sua sessão expirou. Entre de novo.", type="warning")
        ui.navigate.to(f"{LOGIN_PATH}?erro=sessao_expirada")
    except (ContractError, BackendError) as exc:
        notify_error(exc)
