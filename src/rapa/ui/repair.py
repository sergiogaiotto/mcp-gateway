"""Tela "Consertar este passo", a partir de uma execução que falhou.

Mostra o passo que quebrou (como estava), os candidatos que o código enumerou a partir
do retrato da página e a sugestão do modelo de linguagem, que só escolhe um deles. Nada
vem escolhido: quem decide é a pessoa, e a versão nova é um rascunho que outra pessoa,
com o papel de aprovação, valida e publica (na tela do robô). Sem o hub, a tela mostra
os candidatos com um aviso. Todo texto da página entra por ``ui.label`` (escapado).
"""

from uuid import UUID

from nicegui import ui

from rapa.core.contracts import (
    RepairCandidate,
    RepairSuggestionStatus,
    RepairView,
    RunState,
    RunStatus,
)
from rapa.core.snapshot import SNAPSHOT_ERROR_CODES
from rapa.ui.context import Viewer
from rapa.ui.layout import effect_badge, run_action, simple_table
from rapa.ui.permissions import can_create

__all__ = ["can_repair", "repair_page"]

_SUGGESTION_COLORS = {
    RepairSuggestionStatus.CHOSEN: "bg-green-1",
    RepairSuggestionStatus.NONE: "bg-amber-1",
    RepairSuggestionStatus.UNAVAILABLE: "bg-amber-1",
    RepairSuggestionStatus.REFUSED: "bg-amber-1",
    RepairSuggestionStatus.OFF: "bg-grey-2",
}
_LOCATOR_LABELS = {
    "role": "papel",
    "label": "rótulo",
    "placeholder": "placeholder",
    "text": "texto",
    "test_id": "test id",
    "css": "CSS",
    "xpath": "XPath",
}


def can_repair(status: RunStatus) -> bool:
    """Se a execução falhou de um jeito que o conserto assistido trata (o servidor confere)."""
    return status.state is RunState.FAILED and status.error_code in SNAPSHOT_ERROR_CODES


def _parse_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


def _locator_text(by: str, value: str, name: str | None) -> str:
    label = _LOCATOR_LABELS.get(by, by)
    return f"{label}: {value} “{name}”" if name else f"{label}: {value}"


def _step_card(view: RepairView) -> None:
    step = view.step
    with ui.card().classes("w-full").mark("passo-quebrado"):
        with ui.row().classes("items-center gap-2"):
            ui.label(f"Passo que quebrou: {step.title}").classes("text-h6")
            effect_badge(step.effect)
        ui.label(view.error_message or "O elemento do passo não foi encontrado.").classes(
            "text-negative"
        ).mark("mensagem-falha")
        rows = [
            ["Robô", f"{view.robot_slug} (versão {view.version_no})"],
            ["Tipo do passo", step.kind],
            ["Elemento de antes", step.intent],
            [
                "Localizadores de antes",
                "; ".join(
                    _locator_text(loc.by.value, loc.value, loc.name) for loc in step.locators
                ),
            ],
            ["Página na falha", f"{view.page_title} ({view.page_url})"],
        ]
        if step.within is not None:
            rows.append(["Linha", step.within_has_text or "a mesma linha de antes"])
        simple_table(["", ""], rows)
        if view.truncated:
            ui.label(
                "A página era grande demais: o retrato não guardou todos os elementos."
            ).classes("text-caption text-warning")


def _suggestion_card(view: RepairView) -> None:
    suggestion = view.suggestion
    color = _SUGGESTION_COLORS[suggestion.status]
    with ui.card().classes(f"w-full {color}").mark(f"sugestao-{suggestion.status.value}"):
        ui.label("Sugestão do modelo de linguagem").classes("text-subtitle1")
        ui.label(suggestion.message).mark("sugestao-mensagem")
        if suggestion.reason:
            ui.label(f"Motivo dado pelo modelo: {suggestion.reason}").classes("text-caption")


def _candidate_label(candidate: RepairCandidate, suggested: str | None) -> str:
    parts = [f"{candidate.candidate_id}: {candidate.description}"]
    if candidate.candidate_id == suggested:
        parts.append("(sugerido pelo modelo)")
    parts.append(f"nota {candidate.score}")
    return " ".join(parts)


def _candidates_card(view: RepairView) -> None:
    suggested = (
        view.suggestion.candidate_id
        if view.suggestion.status is RepairSuggestionStatus.CHOSEN
        else None
    )
    with ui.card().classes("w-full").mark("candidatos"):
        ui.label("Candidatos").classes("text-h6")
        if not view.candidates:
            ui.label("Nenhum elemento da página serve a este passo. Grave o robô de novo.").mark(
                "sem-candidatos"
            )
            return
        ui.label(
            "Os elementos da página que podem ser o alvo do passo, do mais provável para o "
            "menos provável. Nada foi executado de novo para montar esta lista."
        ).classes("text-caption")
        for candidate in view.candidates:
            with (
                ui.column()
                .classes("w-full gap-0 border rounded p-2")
                .mark(f"candidato-{candidate.candidate_id}")
            ):
                ui.label(_candidate_label(candidate, suggested)).classes("text-weight-medium")
                details = [
                    f"{name}: {value}"
                    for name, value in (
                        ("papel", candidate.role or candidate.tag),
                        ("nome", candidate.name),
                        ("rótulo", candidate.label),
                        ("placeholder", candidate.placeholder),
                        ("texto", candidate.text),
                        ("test id", candidate.test_id),
                        ("linha", candidate.row_text),
                    )
                    if value
                ]
                ui.label(" · ".join(details)).classes("text-caption break-words")
                ui.label(
                    "Localizadores novos: "
                    + "; ".join(
                        _locator_text(loc.by.value, loc.value, loc.name)
                        for loc in candidate.target.locators
                    )
                ).classes("text-caption")
                if candidate.frames:
                    ui.label(f"Dentro do iframe {' > '.join(candidate.frames)}").classes(
                        "text-caption"
                    )
                if candidate.reasons:
                    ui.label("; ".join(candidate.reasons)).classes("text-caption text-grey-8")


def _choice_card(viewer: Viewer, run_id: UUID, view: RepairView, result: ui.column) -> None:
    with ui.card().classes("w-full").mark("escolher-conserto"):
        ui.label("Escolha o elemento").classes("text-h6")
        ui.label(
            "A versão nova é um rascunho só com este passo trocado. Outra pessoa, com o papel "
            "de aprovação, valida e publica: ninguém aprova a própria versão."
        ).classes("text-caption")
        if view.blocked_reason is not None:
            ui.label(view.blocked_reason).classes("text-warning").mark("conserto-bloqueado")
            return
        if not view.candidates:
            return
        if not can_create(viewer.principal, view.area_id):
            ui.label("Só quem cria robôs nesta área pode pedir o conserto.").mark(
                "sem-permissao-conserto"
            )
            return
        choice = ui.radio(
            {c.candidate_id: _candidate_label(c, None) for c in view.candidates}
        ).mark("escolha-candidato")

        async def create() -> None:
            candidate_id = choice.value
            if not candidate_id:
                ui.notify("Escolha um dos candidatos.", type="warning")
                return
            backend = await viewer.backend()
            proposal = await backend.create_repair(run_id, str(candidate_id))
            button.disable()
            result.clear()
            with result, ui.card().classes("w-full bg-green-1").mark("conserto-criado"):
                ui.label(
                    f"Versão {proposal.version_no} criada como rascunho, com o passo "
                    f"'{proposal.step_id}' consertado."
                )
                ui.label(
                    "Agora alguém com o papel de aprovação valida e publica a versão nova."
                ).classes("text-caption")
                ui.link(
                    "Abrir a versão nova",
                    f"/robos/{proposal.robot_id}?versao={proposal.version_no}",
                ).mark("abrir-versao-consertada")

        button = ui.button("Criar a versão consertada", on_click=lambda: run_action(create)).mark(
            "criar-conserto"
        )


def _proposal_card(view: RepairView) -> None:
    proposal = view.proposal
    if proposal is None:
        return
    with ui.card().classes("w-full bg-blue-1").mark("conserto-pedido"):
        ui.label(
            f"Conserto já pedido: versão {proposal.version_no}, com o candidato "
            f"{proposal.candidate_id}, por {proposal.requested_by}."
        )
        ui.link(
            "Abrir a versão consertada",
            f"/robos/{proposal.robot_id}?versao={proposal.version_no}",
        ).mark("abrir-versao-consertada")


async def repair_page(viewer: Viewer, run_id: str) -> None:
    parsed = _parse_uuid(run_id)
    backend = await viewer.backend()
    status = await backend.get_run(parsed) if parsed is not None else None
    if status is None or parsed is None:
        with ui.card().classes("w-full").mark("nao-encontrado"):
            ui.label("Execução não encontrada.")
        return
    ui.label("Consertar este passo").classes("text-h5").mark("titulo-conserto")
    ui.link("Voltar à execução", f"/execucoes/{parsed}")
    view = await backend.get_repair(parsed)
    _step_card(view)
    _proposal_card(view)
    _suggestion_card(view)
    _candidates_card(view)
    result = ui.column().classes("w-full")
    _choice_card(viewer, parsed, view, result)
