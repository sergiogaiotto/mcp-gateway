"""Telas de gravação: Gravar (abrir uma sessão), a visão ao vivo e a revisão.

A interface continua fina: as regras (quem pode gravar, limites, prazos, o que
entra no robô) ficam no plano de controle, e estas telas só usam o
:class:`rapa.ui.backend.Backend`. A visão ao vivo é o único lugar com JavaScript
nosso (``static/ao_vivo.js``): ele desenha a tela num ``<canvas>``, captura mouse,
roda e teclado (com acentos, composição e colar) e fala direto com o websocket da
API com um bilhete de uso único, que a página pede ao backend. O token de acesso
nunca chega ao navegador.

A revisão mostra cada proposta (entradas, nomes de passos, repetições, efeitos,
conferências e o título e a descrição sugeridos pelo modelo) com "Aceitar" ou
"Rejeitar" (começa em "Rejeitar": nada entra sem a pessoa aceitar) e campos para
editar; o título e a descrição do formulário começam com os da compilação. "Criar
rascunho" manda só o que foi aceito.

Os diálogos da página do portal (``alert``, ``confirm``, ``prompt``) aparecem dentro
da tela ao vivo, com a origem escrita, nunca como diálogos do navegador da pessoa.
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from nicegui import ui

from rapa.core.contracts import (
    ContractError,
    DraftChoices,
    EffectChoice,
    GeneralizationProposals,
    InvalidInput,
    ParameterChoice,
    RecordingSessionInfo,
    RecordingSessionStatus,
    RepetitionChoice,
    RepetitionFieldChoice,
    StepNameChoice,
    ViewerTicket,
)
from rapa.core.robot import Effect
from rapa.ui.backend import BackendError
from rapa.ui.context import Viewer
from rapa.ui.labels import (
    EFFECT_LABELS,
    END_REASON_LABELS,
    PARAMETER_TYPES,
    RECORDING_STATUS_COLORS,
    RECORDING_STATUS_LABELS,
    format_datetime,
)
from rapa.ui.layout import effect_badge, notify_error, run_action
from rapa.ui.permissions import can_create_anywhere, can_record, creator_areas

__all__ = [
    "LIVE_SCRIPT_PATH",
    "STATIC_DIR",
    "STATIC_PATH",
    "ReviewForm",
    "live_page",
    "record_page",
    "review_page",
]

#: Pasta e rota dos arquivos estáticos da interface (o JavaScript da visão ao vivo).
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_PATH = "/rapa-estatico"
LIVE_SCRIPT_PATH = f"{STATIC_PATH}/ao_vivo.js"

_ACCEPT, _REJECT = "aceitar", "rejeitar"
_CHOICE = {_ACCEPT: "Aceitar", _REJECT: "Rejeitar"}
_SESSIONS_SHOWN = 20


def _parse_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


def _status_badge(status: RecordingSessionStatus) -> ui.badge:
    return ui.badge(RECORDING_STATUS_LABELS[status], color=RECORDING_STATUS_COLORS[status]).mark(
        "situacao-gravacao"
    )


def _remaining(expires_at: datetime) -> str:
    seconds = int((expires_at - datetime.now(UTC)).total_seconds())
    if seconds <= 0:
        return "Tempo esgotado"
    minutes, seconds = divmod(seconds, 60)
    return f"Tempo restante: {minutes:02d}:{seconds:02d}"


def _not_found(text: str) -> None:
    with ui.card().classes("w-full").mark("nao-encontrado"):
        ui.label(text)


# ---------------------------------------------------------------------------
# Gravar
# ---------------------------------------------------------------------------


async def record_page(viewer: Viewer) -> None:
    """Abrir uma gravação e ver as gravações da própria pessoa."""
    ui.label("Gravar uma demonstração").classes("text-h5")
    principal = viewer.principal
    if not can_record(principal):
        with ui.card().classes("w-full").mark("sem-permissao-gravar"):
            ui.label("Você não tem permissão para gravar demonstrações.")
            ui.label(
                "Só quem tem o papel de criação de robôs numa área pode gravar. "
                "Peça o papel ao administrador da sua empresa."
            ).classes("text-caption")
        return
    backend = await viewer.backend()
    sessions = await backend.list_sessions(_SESSIONS_SHOWN)
    with ui.card().classes("w-full").mark("nova-gravacao"):
        ui.label("Nova gravação").classes("text-h6")
        ui.label(
            "Um navegador na nuvem abre o endereço e você faz a tarefa uma vez, vendo a tela "
            "aqui. Senhas e códigos que você digitar nunca entram na gravação: o robô usa o "
            "cofre. A gravação termina depois de 10 minutos sem atividade ou de 60 minutos."
        ).classes("text-caption")
        areas = creator_areas(principal, [s.area_id for s in sessions])
        options = {str(area): viewer.area_label(area) for area in areas}
        area_select = (
            ui.select(options, label="Área", value=next(iter(options), None))
            .classes("w-96")
            .mark("area-gravacao")
        )
        other_area = (
            ui.input("Ou o identificador de outra área").classes("w-96").mark("outra-area-gravacao")
            if can_create_anywhere(principal)
            else None
        )
        address = (
            ui.input("Endereço inicial", placeholder="https://portal.exemplo.gov.br/login")
            .classes("w-full")
            .mark("endereco-inicial")
        )
        title = ui.input("Nome da tarefa (opcional)").classes("w-96").mark("titulo-gravacao")

        async def start() -> None:
            raw_area = (other_area.value or "").strip() if other_area is not None else ""
            area_id = _parse_uuid(raw_area) if raw_area else _parse_uuid(area_select.value or "")
            if area_id is None:
                ui.notify("Escolha uma área válida.", type="warning")
                return
            url = (address.value or "").strip()
            if not url:
                ui.notify("Informe o endereço inicial.", type="warning")
                return
            current = await viewer.backend()
            info = await current.create_session(area_id, url, (title.value or "").strip())
            ui.notify("Navegador aberto. Pode começar.", type="positive")
            ui.navigate.to(f"/gravar/{info.session_id}")

        ui.button("Começar a gravar", on_click=lambda: run_action(start)).mark("comecar-gravacao")
    with ui.card().classes("w-full").mark("lista-gravacoes"):
        ui.label("Suas gravações").classes("text-h6")
        if not sessions:
            ui.label("Nenhuma gravação ainda.").mark("sem-gravacoes")
        for info in sessions:
            _session_row(viewer, info)


def _session_row(viewer: Viewer, info: RecordingSessionInfo) -> None:
    sid = info.session_id
    with ui.row().classes("w-full items-center justify-between").mark(f"gravacao-{sid}"):
        with ui.column().classes("gap-0"):
            with ui.row().classes("items-center gap-2"):
                ui.label(info.title or "Gravação sem nome").classes("text-weight-medium")
                _status_badge(info.status)
            ui.label(
                f"{info.start_url} · {viewer.area_label(info.area_id)} · "
                f"{format_datetime(info.created_at)}"
            ).classes("text-caption break-all")
            if info.end_reason and not info.status.live:
                ui.label(END_REASON_LABELS.get(info.end_reason, "Encerrada.")).classes(
                    "text-caption text-warning"
                )
        with ui.row().classes("gap-2"):
            if info.status is RecordingSessionStatus.ACTIVE:
                ui.button("Abrir", on_click=lambda: ui.navigate.to(f"/gravar/{sid}")).props(
                    "flat"
                ).mark(f"abrir-gravacao-{sid}")
            if info.status.reviewable:
                ui.button(
                    "Revisar", on_click=lambda: ui.navigate.to(f"/gravar/{sid}/revisar")
                ).props("flat").mark(f"revisar-gravacao-{sid}")
            if info.robot_id is not None and info.version_no is not None:
                target = f"/robos/{info.robot_id}?versao={info.version_no}"
                ui.button("Ver robô", on_click=lambda: ui.navigate.to(target)).props("flat").mark(
                    f"robo-gravacao-{sid}"
                )


# ---------------------------------------------------------------------------
# Visão ao vivo
# ---------------------------------------------------------------------------


def _live_config(viewer: Viewer, info: RecordingSessionInfo, ticket: ViewerTicket) -> str:
    base = viewer.ctx.settings.live_ws_base
    config = {
        "sessao": str(info.session_id),
        "caminho": ticket.path,
        "bilhete": ticket.ticket,
        "base": base.rstrip("/") if base else None,
        "expira_em": info.expires_at.isoformat(),
    }
    # JSON dentro de <script>: nada de "<" cru (um "</script>" fecharia o bloco).
    data = json.dumps(config, ensure_ascii=False).replace("<", "\\u003c")
    return f"<script>window.RAPA_AO_VIVO = {data};</script>"


async def live_page(viewer: Viewer, session_id: str) -> None:
    """A tela do navegador da nuvem, com o mouse e o teclado da pessoa."""
    sid = _parse_uuid(session_id)
    backend = await viewer.backend()
    info = await backend.get_session(sid) if sid is not None else None
    if info is None or sid is None:
        _not_found("Gravação não encontrada.")
        return
    active = info.status is RecordingSessionStatus.ACTIVE
    if active:
        # O bilhete e os scripts vêm antes de tudo o que manda a página ao navegador. O
        # ui.timer do tempo restante espera a conexão: com ele criado, o próximo await
        # (o bilhete, que vai ao banco ou à API) deixa o NiceGUI responder com a página
        # pela metade, e o ui.add_body_html seguinte vira um insertAdjacentHTML, que não
        # executa <script> (a tela ao vivo nunca começaria).
        ticket = await backend.issue_ticket(sid)
        ui.add_body_html(_live_config(viewer, info, ticket))
        ui.add_body_html(f'<script src="{LIVE_SCRIPT_PATH}" defer></script>')
    with ui.row().classes("w-full items-center justify-between"):
        with ui.row().classes("items-center gap-2"):
            ui.label(info.title or "Gravação").classes("text-h5").mark("titulo-gravacao-ao-vivo")
            _status_badge(info.status)
        if active:
            timer_label = (
                ui.label(_remaining(info.expires_at))
                .classes("text-weight-medium")
                .mark("tempo-restante")
            )
            ui.timer(1.0, lambda: timer_label.set_text(_remaining(info.expires_at)))
    if not active:
        with ui.card().classes("w-full").mark("gravacao-fechada"):
            if info.status.reviewable:
                ui.label("Esta gravação já terminou e está guardada.")
                ui.button(
                    "Revisar", on_click=lambda: ui.navigate.to(f"/gravar/{sid}/revisar")
                ).mark("ir-revisar")
            else:
                reason = END_REASON_LABELS.get(info.end_reason or "", "Esta gravação terminou.")
                ui.label(reason)
                ui.button("Gravar de novo", on_click=lambda: ui.navigate.to("/gravar")).mark(
                    "gravar-de-novo"
                )
        return
    ui.label(
        "Clique na tela para usar o mouse e o teclado do navegador da nuvem. Acentos, colar "
        "(Ctrl+V) e a barra de endereço funcionam. Senhas digitadas aqui não entram na gravação. "
        "Sem atividade por 10 minutos, a gravação termina e o que foi feito se perde."
    ).classes("text-caption")
    status = ui.label("").classes("text-caption text-warning").mark("aviso-gravacao")
    with ui.row().classes("w-full items-center gap-2"):
        stop_button = ui.button("Parar e revisar", color="negative").mark("parar-e-revisar")
    ui.element("div").classes("w-full").props("id=rapa-ao-vivo").mark("tela-ao-vivo")

    async def stop() -> None:
        stop_button.disable()
        status.set_text("Parando e guardando a gravação…")
        try:
            current = await viewer.backend()
            await current.stop_session(sid)
        except ContractError, BackendError:
            stop_button.enable()
            status.set_text("")
            raise
        ui.navigate.to(f"/gravar/{sid}/revisar")

    stop_button.on_click(lambda: run_action(stop))

    async def new_ticket() -> None:
        # O JavaScript pede outro bilhete para reconectar uma vez (cada bilhete vale uma vez).
        try:
            current = await viewer.backend()
            fresh = await current.issue_ticket(sid)
        except ContractError, BackendError:
            ui.run_javascript("window.RapaAoVivo && window.RapaAoVivo.semBilhete()")
            return
        data = json.dumps(fresh.ticket)
        ui.run_javascript(f"window.RapaAoVivo && window.RapaAoVivo.bilhete({data})")

    def ended(event: Any) -> None:
        args = event.args
        if isinstance(args, list | tuple):
            args = args[0] if args else ""
        reason = args if isinstance(args, str) else ""
        if reason == "parada":
            ui.navigate.to(f"/gravar/{sid}/revisar")
        elif reason in END_REASON_LABELS:
            status.set_text(END_REASON_LABELS[reason])

    ui.on("rapa_novo_bilhete", new_ticket)
    ui.on("rapa_sessao_fim", ended)


# ---------------------------------------------------------------------------
# Revisão
# ---------------------------------------------------------------------------


def _decision(marker: str) -> ui.toggle:
    return ui.toggle(_CHOICE, value=_REJECT).props("dense no-caps").mark(marker)


@dataclass
class _ParameterRow:
    step_id: str
    decision: ui.toggle
    name: ui.input
    title: ui.input
    type: ui.select


@dataclass
class _NameRow:
    step_id: str
    current: str
    title: ui.input
    decision: ui.toggle | None = None


@dataclass
class _RepetitionRow:
    proposal_id: str
    step_ids: list[str]
    body_step_ids: list[str]
    decision: ui.toggle
    list_name: ui.input
    list_title: ui.input
    fields: list[tuple[str, ui.input, ui.input, ui.select]] = field(default_factory=list)


@dataclass
class ReviewForm:
    """As escolhas da pessoa na revisão, lidas dos campos da tela."""

    proposals: GeneralizationProposals
    name: ui.input | None = None
    title: ui.input | None = None
    description: ui.textarea | None = None
    parameters: list[_ParameterRow] = field(default_factory=list)
    names: list[_NameRow] = field(default_factory=list)
    repetitions: list[_RepetitionRow] = field(default_factory=list)
    effects: list[tuple[str, str, ui.toggle]] = field(default_factory=list)
    checks: list[tuple[str, ui.toggle]] = field(default_factory=list)

    def choices(self) -> DraftChoices:
        """O que foi aceito, com as edições; o servidor confere tudo de novo."""
        if self.name is None or self.title is None or self.description is None:
            raise ValueError("o formulário da revisão não foi montado")
        repetitions = [r for r in self.repetitions if r.decision.value == _ACCEPT]
        # Os passos das voltas seguintes saem do robô; os valores que mudam viram itens.
        removed = {s for r in repetitions for s in r.step_ids if s not in r.body_step_ids}
        itemized = {step for r in repetitions for step, *_ in r.fields}
        return DraftChoices(
            name=(self.name.value or "").strip(),
            title=(self.title.value or "").strip(),
            description=(self.description.value or "").strip(),
            repetitions=[
                RepetitionChoice(
                    proposal_id=r.proposal_id,
                    list_name=(r.list_name.value or "").strip(),
                    list_title=(r.list_title.value or "").strip(),
                    fields=[
                        RepetitionFieldChoice(
                            step_id=step,
                            name=(name.value or "").strip(),
                            title=(title.value or "").strip(),
                            type=kind.value,
                        )
                        for step, name, title, kind in r.fields
                    ],
                )
                for r in repetitions
            ],
            parameters=[
                ParameterChoice(
                    step_id=p.step_id,
                    name=(p.name.value or "").strip(),
                    title=(p.title.value or "").strip(),
                    type=p.type.value,
                )
                for p in self.parameters
                if p.decision.value == _ACCEPT
                and p.step_id not in removed
                and p.step_id not in itemized
            ],
            step_names=[
                StepNameChoice(step_id=n.step_id, title=(n.title.value or "").strip())
                for n in self.names
                if n.step_id not in removed
                and (n.decision.value == _ACCEPT if n.decision is not None else True)
                and (n.title.value or "").strip()
                and (n.title.value or "").strip() != n.current
            ],
            effects=[
                EffectChoice(step_id=step, effect=effect)
                for step, effect, decision in self.effects
                if decision.value == _ACCEPT and step not in removed
            ],
            checks=[text for text, decision in self.checks if decision.value == _ACCEPT],
        )


def _type_select(value: str, marker: str) -> ui.select:
    return ui.select(PARAMETER_TYPES, value=value, label="Tipo").classes("w-40").mark(marker)


def _source(source: Any) -> None:
    text = (
        "sugestão do modelo" if getattr(source, "value", "") == "modelo" else "sugestão automática"
    )
    ui.badge(text, color="blue-grey-5").props("outline")


async def review_page(viewer: Viewer, session_id: str) -> None:
    """Revisão da gravação: aceitar, editar ou rejeitar cada proposta e criar o rascunho."""
    sid = _parse_uuid(session_id)
    backend = await viewer.backend()
    info = await backend.get_session(sid) if sid is not None else None
    if info is None or sid is None:
        _not_found("Gravação não encontrada.")
        return
    ui.label(f"Revisar: {info.title or 'gravação'}").classes("text-h5").mark("titulo-revisao")
    if not info.status.reviewable:
        with ui.card().classes("w-full").mark("revisao-indisponivel"):
            ui.label("Esta gravação não tem o que revisar.")
            if info.end_reason:
                ui.label(END_REASON_LABELS.get(info.end_reason, "")).classes("text-caption")
        return
    proposals = await backend.get_proposals(sid)
    form = ReviewForm(proposals)
    if not proposals.model_available:
        with ui.card().classes("w-full bg-orange-1").mark("modelo-indisponivel"):
            ui.label(proposals.model_message or "As propostas do modelo estão indisponíveis.")
    if info.robot_id is not None:
        ui.label(
            f"Esta gravação já virou a versão {info.version_no} de um robô; criar de novo faz "
            "uma versão nova."
        ).classes("text-caption").mark("ja-tem-rascunho")
    ui.label(
        "Nada entra no robô sem você aceitar. Edite o que quiser antes de criar o rascunho; o "
        "RaPA confere cada escolha e o portão de política confere o robô."
    ).classes("text-caption")
    if proposals.warnings:
        with ui.card().classes("w-full").mark("avisos-revisao"):
            ui.label("Avisos da compilação").classes("text-subtitle1")
            for warning in proposals.warnings:
                ui.label(f"• {warning}").classes("text-caption")
    with ui.card().classes("w-full").mark("dados-robo"):
        ui.label("Robô").classes("text-h6")
        form.name = (
            ui.input("Nome técnico", value=proposals.name).classes("w-96").mark("nome-tecnico")
        )
        form.title = (
            ui.input("Título", value=proposals.title).classes("w-full").mark("titulo-robo-revisao")
        )
        form.description = (
            ui.textarea("Descrição", value=proposals.description)
            .classes("w-full")
            .mark("descricao-robo")
        )
        _robot_info_suggestion(form)
    _repetitions_card(form)
    _parameters_card(form)
    _names_card(form)
    _effects_card(form)
    errors = ui.column().classes("w-full text-negative").mark("erros-revisao")

    async def create() -> None:
        errors.clear()
        try:
            choices = form.choices()
        except ValueError:
            ui.notify(
                "Confira os campos: os nomes técnicos usam só letras minúsculas, números e _ "
                "(sem acentos nem espaços), e os títulos e a descrição não podem ficar vazios.",
                type="warning",
                multi_line=True,
            )
            return
        current = await viewer.backend()
        try:
            version = await current.create_draft(sid, choices)
        except InvalidInput as exc:
            with errors:
                ui.label(exc.message[:1].upper() + exc.message[1:])
                for detail in exc.errors:
                    ui.label(detail).classes("text-caption")
            notify_error(exc)
            return
        ui.notify(f"Rascunho criado: versão {version.version_no}.", type="positive")
        ui.navigate.to(f"/robos/{version.robot_id}?versao={version.version_no}")

    ui.button("Criar rascunho", on_click=lambda: run_action(create), color="primary").mark(
        "criar-rascunho"
    )
    if proposals.model_rejected:
        with (
            ui.expansion("O que o modelo propôs e o RaPA recusou")
            .classes("w-full")
            .mark("recusadas-modelo")
        ):
            for reason in proposals.model_rejected:
                ui.label(f"• {reason}").classes("text-caption")


def _robot_info_suggestion(form: ReviewForm) -> None:
    """A sugestão do modelo para o título e a descrição: à parte, e só entra se aceita.

    Aceitar põe o texto do modelo nos campos (que continuam editáveis); rejeitar volta
    os da compilação, se a pessoa não os mudou depois.
    """
    proposals = form.proposals
    suggestion = proposals.robot_info
    if suggestion is None or form.title is None or form.description is None:
        return
    title_field, description_field = form.title, form.description
    with ui.column().classes("w-full gap-1 p-2 bg-blue-grey-1").mark("sugestao-robo"):
        ui.label("Sugestão do modelo para quem aprova").classes("text-weight-medium")
        if suggestion.title:
            ui.label(f"Título: {suggestion.title}").mark("sugestao-robo-titulo")
        if suggestion.description:
            ui.label(f"Descrição: {suggestion.description}").classes("break-all").mark(
                "sugestao-robo-descricao"
            )
        with ui.row().classes("items-center gap-2"):
            decision = _decision("decisao-sugestao-robo")
            _source(suggestion.source)

    def apply(event: Any) -> None:
        accepted = event.value == _ACCEPT
        for target, suggested, compiled in (
            (title_field, suggestion.title, proposals.title),
            (description_field, suggestion.description, proposals.description),
        ):
            if not suggested:
                continue
            if accepted:
                target.value = suggested
            elif target.value == suggested:
                target.value = compiled

    decision.on_value_change(apply)


def _repetitions_card(form: ReviewForm) -> None:
    proposals = form.proposals
    if not proposals.repetitions:
        return
    titles = {s.step_id: s for s in proposals.steps}
    with ui.card().classes("w-full").mark("repeticoes"):
        ui.label("Repetições").classes("text-h6")
        ui.label(
            "Você fez os mesmos passos mais de uma vez com valores diferentes. Aceite para o robô "
            "repetir esses passos para cada item de uma lista."
        ).classes("text-caption")
        for rep in proposals.repetitions:
            with ui.column().classes("w-full gap-1 p-2").mark(f"repeticao-{rep.proposal_id}"):
                body = [titles[s].title for s in rep.body_step_ids if s in titles]
                ui.label(f"{rep.times} vezes: " + " → ".join(body)).classes("text-weight-medium")
                with ui.row().classes("items-center gap-2"):
                    decision = _decision(f"decisao-repeticao-{rep.proposal_id}")
                    _source(rep.source)
                with ui.row().classes("items-center gap-2"):
                    list_name = ui.input("Nome da lista", value=rep.list_name).mark(
                        f"lista-nome-{rep.proposal_id}"
                    )
                    list_title = (
                        ui.input("Título da lista", value=rep.list_title)
                        .classes("w-72")
                        .mark(f"lista-titulo-{rep.proposal_id}")
                    )
                row = _RepetitionRow(
                    rep.proposal_id,
                    rep.step_ids,
                    rep.body_step_ids,
                    decision,
                    list_name,
                    list_title,
                )
                for item in rep.fields:
                    with ui.row().classes("items-center gap-2 pl-4"):
                        name = ui.input("Campo", value=item.name).mark(f"campo-nome-{item.step_id}")
                        title = ui.input("Título", value=item.title).mark(
                            f"campo-titulo-{item.step_id}"
                        )
                        kind = _type_select(item.type, f"campo-tipo-{item.step_id}")
                        ui.label("valores: " + ", ".join(item.values)).classes("text-caption")
                    row.fields.append((item.step_id, name, title, kind))
                form.repetitions.append(row)


def _parameters_card(form: ReviewForm) -> None:
    proposals = form.proposals
    with ui.card().classes("w-full").mark("parametros"):
        ui.label("Entradas do robô").classes("text-h6")
        if not proposals.parameters:
            ui.label("Nenhum valor digitado que possa virar entrada.").classes("text-caption")
            return
        ui.label(
            "Valores que você digitou ou escolheu. Aceite os que mudam a cada execução (um CPF, "
            "um nome): eles viram entradas do robô. Os passos de uma repetição aceita ficam de fora."
        ).classes("text-caption")
        for param in proposals.parameters:
            with ui.column().classes("w-full gap-1 p-2").mark(f"parametro-{param.step_id}"):
                label = param.field_label or param.step_title
                ui.label(f"{label}: “{param.value_preview}”").classes("text-weight-medium")
                with ui.row().classes("items-center gap-2"):
                    decision = _decision(f"decisao-parametro-{param.step_id}")
                    _source(param.source)
                with ui.row().classes("items-center gap-2"):
                    name = ui.input("Nome da entrada", value=param.name).mark(
                        f"parametro-nome-{param.step_id}"
                    )
                    title = (
                        ui.input("Título", value=param.title)
                        .classes("w-72")
                        .mark(f"parametro-titulo-{param.step_id}")
                    )
                    kind = _type_select(param.type, f"parametro-tipo-{param.step_id}")
                form.parameters.append(_ParameterRow(param.step_id, decision, name, title, kind))


def _names_card(form: ReviewForm) -> None:
    proposals = form.proposals
    suggested = {p.step_id: p for p in proposals.step_names}
    with ui.card().classes("w-full").mark("nomes-passos"):
        ui.label("Passos").classes("text-h6")
        ui.label(
            "Os nomes que aparecem para quem aprova e para quem executa. As sugestões do modelo "
            "só entram se você aceitar; os outros nomes você pode editar direto."
        ).classes("text-caption")
        for step in proposals.steps:
            with (
                ui.row()
                .classes("w-full items-center gap-2 no-wrap")
                .style(f"margin-left: {step.depth * 24}px")
                .mark(f"passo-revisao-{step.step_id}")
            ):
                ui.label(f"{step.number}.").classes("text-weight-medium")
                proposal = suggested.get(step.step_id)
                title = (
                    ui.input(value=proposal.title if proposal else step.title)
                    .classes("w-96")
                    .mark(f"nome-passo-{step.step_id}")
                )
                if step.effect.value != "read":
                    effect_badge(step.effect)
                decision = None
                if proposal is not None:
                    decision = _decision(f"decisao-nome-{step.step_id}")
                    ui.label(f"(hoje: {proposal.current_title})").classes("text-caption")
                form.names.append(_NameRow(step.step_id, step.title, title, decision))


def _effects_card(form: ReviewForm) -> None:
    proposals = form.proposals
    if not proposals.effects and not proposals.checks:
        return
    with ui.card().classes("w-full").mark("efeitos-conferencias"):
        if proposals.effects:
            ui.label("Efeitos").classes("text-h6")
            ui.label(
                "O modelo acha que estes passos gravam mais do que o RaPA concluiu. O efeito só "
                "sobe: um passo irreversível pede confirmação a cada execução."
            ).classes("text-caption")
            for effect in proposals.effects:
                with ui.row().classes("items-center gap-2").mark(f"efeito-{effect.step_id}"):
                    ui.label(
                        f"{effect.step_title}: {EFFECT_LABELS[effect.current]} → "
                        f"{EFFECT_LABELS[Effect(effect.proposed)]}"
                    )
                    decision = _decision(f"decisao-efeito-{effect.step_id}")
                form.effects.append((effect.step_id, effect.proposed, decision))
        if proposals.checks:
            ui.label("Conferências no fim").classes("text-h6")
            for n, check in enumerate(proposals.checks):
                with ui.row().classes("items-center gap-2").mark(f"conferencia-{n}"):
                    ui.label(f"A página mostra “{check.text}”")
                    decision = _decision(f"decisao-conferencia-{n}")
                form.checks.append((check.text, decision))
