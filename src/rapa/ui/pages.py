"""Páginas da interface.

Toda página confere a sessão no servidor antes de desenhar qualquer coisa e
usa o backend no nome do principal da sessão; a empresa nunca vem da
requisição. As ações dos botões conferem a sessão de novo a cada clique.
"""

import json
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote
from uuid import UUID

from fastapi import Request
from fastapi.responses import RedirectResponse, Response
from nicegui import ui

from rapa.core.contracts import (
    ContractError,
    InvalidInput,
    RobotInfo,
    RobotSummary,
    RobotVersionInfo,
    RunState,
    RunStatus,
    VersionStatus,
)
from rapa.core.ids import new_id
from rapa.core.robot import Effect, Manifest
from rapa.ui import describe
from rapa.ui.auth import BINDING_COOKIE, HOME_PATH, LOGIN_ERRORS, LoginError, safe_return_path
from rapa.ui.backend import BackendError, BackendUnavailable, NotAuthenticated
from rapa.ui.company import company_page
from rapa.ui.context import UiContext, Viewer
from rapa.ui.credentials import A1Context, a1_card, credentials_page, expiry_banner, load_a1
from rapa.ui.evidence import evidence_card
from rapa.ui.forms import SchemaForm
from rapa.ui.labels import (
    ROBOT_STATUS_LABELS,
    TRIGGER_LABELS,
    VERSION_STATUS_LABELS,
    format_datetime,
)
from rapa.ui.layout import (
    effect_badge,
    error_box,
    no_role_frame,
    notify_error,
    page_frame,
    run_action,
    simple_table,
    state_badge,
)
from rapa.ui.permissions import (
    can_create_anywhere,
    can_run,
    creator_areas,
    has_any_role,
    publish_block_reason,
    validate_block_reason,
)
from rapa.ui.publishing import (
    batch_page,
    batches_page,
    schedule_page,
    schedules_page,
    webhooks_page,
)
from rapa.ui.recording import live_page, record_page, review_page
from rapa.ui.repair import can_repair, can_repair_code, repair_page

#: Quantas execuções a tela Execuções mostra.
RECENT_RUNS = 50

__all__ = ["RunPanel", "register_pages"]

type _Body = Callable[[Viewer], Awaitable[None]]


def _parse_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


def _parse_int(value: str) -> int | None:
    return int(value) if value.isdigit() and len(value) <= 6 else None


async def _protected(ctx: UiContext, request: Request, active: str, body: _Body) -> Response | None:
    """Confere a sessão, desenha o cabeçalho e o corpo; mostra os erros em pt-BR."""
    try:
        viewer = await ctx.viewer(request)
    except BackendUnavailable as exc:
        # Sem conferir a conta no banco, a página não abre (nem com os papéis antigos).
        with ui.column().classes("w-full max-w-xl mx-auto p-6 gap-4"):
            error_box(exc)
        return None
    if viewer is None:
        return ctx.login_redirect(request)
    if not has_any_role(viewer.principal):
        # O primeiro login cria a pessoa sem papel: ela só vê o pedido para ganhar um.
        no_role_frame(viewer)
        return None
    try:
        # Os nomes da empresa e das áreas, para o cabeçalho e para o corpo da página.
        await viewer.load_names()
    except NotAuthenticated:
        return ctx.login_redirect(request, expired=True)
    main = page_frame(viewer, active)
    with main:
        content = ui.column().classes("w-full gap-4")
    try:
        with content:
            await expiry_banner(viewer)
            await body(viewer)
    except NotAuthenticated:
        return ctx.login_redirect(request, expired=True)
    except (ContractError, BackendError) as exc:
        content.clear()
        with content:
            error_box(exc)
    return None


def _not_found(text: str) -> None:
    with ui.card().classes("w-full").mark("nao-encontrado"):
        ui.label(text)


def register_pages(ctx: UiContext) -> None:
    """Registra as páginas no NiceGUI (chamado uma vez por ``create_ui``)."""
    timeout = ctx.settings.page_timeout_s
    name = ctx.settings.title

    @ui.page("/", response_timeout=timeout)
    async def index(request: Request) -> Response:
        try:
            signed_in = await ctx.viewer(request) is not None
        except BackendUnavailable:
            signed_in = True  # a página inicial mostra o erro
        if not signed_in:
            return ctx.login_redirect(request)
        return RedirectResponse(HOME_PATH, status_code=303)

    @ui.page("/entrar", title=f"Entrar · {name}", response_timeout=timeout)
    async def login_page(
        request: Request, volta: str = "", erro: str = "", cookie: str = "", saiu: str = ""
    ) -> Response | None:
        return await _login(ctx, request, volta, erro, cookie == "1", saiu == "1")

    @ui.page("/catalogo", title=f"Catálogo · {name}", response_timeout=timeout)
    async def catalog_page(request: Request, q: str = "") -> Response | None:
        return await _protected(ctx, request, "catalogo", lambda v: _catalog(v, q))

    @ui.page("/robos", title=f"Robôs · {name}", response_timeout=timeout)
    async def robots_page(request: Request) -> Response | None:
        return await _protected(ctx, request, "robos", _robots)

    @ui.page("/robos/{robot_id}", title=f"Robô · {name}", response_timeout=timeout)
    async def robot_page(request: Request, robot_id: str, versao: str = "") -> Response | None:
        return await _protected(ctx, request, "robos", lambda v: _robot(v, robot_id, versao))

    @ui.page("/executar/{slug}", title=f"Executar · {name}", response_timeout=timeout)
    async def run_page(request: Request, slug: str) -> Response | None:
        return await _protected(ctx, request, "catalogo", lambda v: _execute(v, slug))

    @ui.page("/execucoes", title=f"Execuções · {name}", response_timeout=timeout)
    async def runs_page(request: Request) -> Response | None:
        return await _protected(ctx, request, "execucoes", _runs)

    @ui.page("/execucoes/{run_id}", title=f"Execução · {name}", response_timeout=timeout)
    async def run_detail_page(request: Request, run_id: str) -> Response | None:
        return await _protected(ctx, request, "execucoes", lambda v: _run_detail(v, run_id))

    @ui.page("/execucoes/{run_id}/conserto", title=f"Consertar · {name}", response_timeout=timeout)
    async def run_repair_page(request: Request, run_id: str) -> Response | None:
        return await _protected(ctx, request, "execucoes", lambda v: repair_page(v, run_id))

    @ui.page("/ao-vivo", title=f"Gravar · {name}", response_timeout=timeout)
    async def live_redirect(request: Request) -> Response:
        # A visão ao vivo é a da gravação; o endereço antigo leva para Gravar.
        return RedirectResponse("/gravar", status_code=303)

    @ui.page("/gravar", title=f"Gravar · {name}", response_timeout=timeout)
    async def record(request: Request) -> Response | None:
        return await _protected(ctx, request, "gravar", record_page)

    @ui.page("/gravar/{session_id}", title=f"Gravando · {name}", response_timeout=timeout)
    async def record_live(request: Request, session_id: str) -> Response | None:
        return await _protected(ctx, request, "gravar", lambda v: live_page(v, session_id))

    @ui.page("/gravar/{session_id}/revisar", title=f"Revisar · {name}", response_timeout=timeout)
    async def record_review(request: Request, session_id: str) -> Response | None:
        return await _protected(ctx, request, "gravar", lambda v: review_page(v, session_id))

    @ui.page("/agendas", title=f"Agendas · {name}", response_timeout=timeout)
    async def schedules(request: Request) -> Response | None:
        return await _protected(ctx, request, "agendas", schedules_page)

    @ui.page("/agendas/{schedule_id}", title=f"Agenda · {name}", response_timeout=timeout)
    async def schedule(request: Request, schedule_id: str) -> Response | None:
        return await _protected(ctx, request, "agendas", lambda v: schedule_page(v, schedule_id))

    @ui.page("/webhooks", title=f"Webhooks · {name}", response_timeout=timeout)
    async def webhooks(request: Request) -> Response | None:
        return await _protected(ctx, request, "webhooks", webhooks_page)

    @ui.page("/lotes", title=f"Lotes · {name}", response_timeout=timeout)
    async def batches(request: Request) -> Response | None:
        return await _protected(ctx, request, "lotes", batches_page)

    @ui.page("/empresa", title=f"Empresa · {name}", response_timeout=timeout)
    async def company(request: Request, mes: str = "") -> Response | None:
        return await _protected(ctx, request, "empresa", lambda v: company_page(v, mes))

    @ui.page("/lotes/{batch_id}", title=f"Lote · {name}", response_timeout=timeout)
    async def batch(request: Request, batch_id: str) -> Response | None:
        return await _protected(ctx, request, "lotes", lambda v: batch_page(v, batch_id))

    @ui.page("/credenciais", title=f"Credenciais · {name}", response_timeout=timeout)
    async def credentials(request: Request) -> Response | None:
        return await _protected(ctx, request, "credenciais", credentials_page)


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


async def _login(
    ctx: UiContext, request: Request, volta: str, erro: str, cookie_tried: bool, logged_out: bool
) -> Response | None:
    return_to = safe_return_path(volta)
    try:
        signed_in = await ctx.viewer(request) is not None
    except BackendUnavailable:
        signed_in = False  # sem conferir a sessão, mostra o login
    if signed_in:
        return RedirectResponse(return_to, status_code=303)
    binding = request.cookies.get(BINDING_COOKIE)
    if binding is None and ctx.auth.dev is not None and not cookie_tried:
        # Primeiro cria o cookie de vínculo, que prende o login a este navegador.
        return RedirectResponse(f"/auth/vinculo?volta={quote(return_to, safe='')}", status_code=303)
    with ui.column().classes("w-full max-w-xl mx-auto p-6 gap-4").mark("pagina-login"):
        ui.label(f"Entrar no {ctx.settings.title}").classes("text-h5")
        if logged_out:
            ui.label("Você saiu. Até logo!").classes("text-positive").mark("saiu")
        if erro:
            message = LOGIN_ERRORS.get(erro, "Não foi possível entrar. Tente de novo.")
            ui.label(message).classes("text-negative").mark("erro-login")
        if binding is None and cookie_tried:
            ui.label(
                "Seu navegador não aceitou o cookie de login. Ative os cookies deste site e tente de novo."
            ).classes("text-negative").mark("sem-cookie")
        if ctx.auth.oidc is not None:
            target = f"/auth/oidc/entrar?volta={quote(return_to, safe='')}"
            ui.button(
                "Entrar com a conta corporativa", on_click=lambda: ui.navigate.to(target)
            ).mark("entrar-oidc")
        dev = ctx.auth.dev
        if dev is not None and binding is not None:
            with ui.card().classes("w-full").mark("login-dev"):
                ui.label("Acesso de desenvolvimento").classes("text-subtitle1")
                ui.label(
                    "Só para desenvolvimento e testes: escolha um dos perfis configurados no servidor."
                ).classes("text-caption")
                for profile in dev.profiles.values():
                    with ui.row().classes("w-full items-center justify-between"):
                        with ui.column().classes("gap-0"):
                            ui.label(profile.label).classes("text-weight-medium")
                            if profile.description:
                                ui.label(profile.description).classes("text-caption")

                        async def choose(key: str = profile.key) -> None:
                            try:
                                ticket = await ctx.auth.create_dev_ticket(binding, key, return_to)
                            except LoginError as exc:
                                ui.notify(LOGIN_ERRORS[exc.code], type="negative")
                                return
                            ui.navigate.to(f"/auth/dev/concluir?bilhete={quote(ticket)}")

                        button = ui.button("Entrar", on_click=choose).mark(f"perfil-{profile.key}")
                        button.props["aria-label"] = f"Entrar como {profile.label}"
    return None


# ---------------------------------------------------------------------------
# Catálogo
# ---------------------------------------------------------------------------


def _robot_card(viewer: Viewer, summary: RobotSummary) -> None:
    with ui.card().classes("w-full").mark(f"robo-{summary.slug}"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.label(summary.title).classes("text-h6")
            with ui.row().classes("items-center gap-2"):
                effect_badge(summary.effect)
                ui.badge(f"versão {summary.version_no}", color="blue-grey-6")
        ui.label(summary.description)
        ui.label(f"Nome técnico: {summary.slug}").classes("text-caption")
        with ui.row().classes("gap-2"):
            if can_run(viewer.principal, summary.area_id):
                ui.button(
                    "Executar",
                    on_click=lambda: ui.navigate.to(f"/executar/{summary.slug}"),
                ).mark(f"executar-{summary.slug}")
            ui.button(
                "Ver passos",
                on_click=lambda: ui.navigate.to(f"/robos/{summary.robot_id}"),
                color="secondary",
            ).props("flat").mark(f"detalhes-{summary.slug}")


async def _catalog(viewer: Viewer, query: str) -> None:
    query = query.strip()[:200]
    backend = await viewer.backend()
    robots = await (backend.search_catalog(query) if query else backend.list_catalog())
    ui.label("Catálogo de robôs").classes("text-h5")
    ui.label("Robôs publicados que você pode usar.").classes("text-caption")
    with ui.row().classes("w-full items-center gap-2"):
        search = ui.input("Buscar robô", value=query).classes("w-96").mark("busca")

        def go() -> None:
            text = (search.value or "").strip()
            ui.navigate.to(f"/catalogo?q={quote(text)}" if text else "/catalogo")

        search.on("keydown.enter", go)
        ui.button("Buscar", on_click=go).mark("buscar")
    with ui.column().classes("w-full gap-3").mark("lista-catalogo"):
        if not robots:
            ui.label(
                "Nenhum robô encontrado para a busca." if query else "Nenhum robô publicado ainda."
            ).mark("catalogo-vazio")
        for summary in robots:
            _robot_card(viewer, summary)


# ---------------------------------------------------------------------------
# Robôs (administração)
# ---------------------------------------------------------------------------


async def _robots(viewer: Viewer) -> None:
    backend = await viewer.backend()
    robots = await backend.list_robots()
    ui.label("Robôs").classes("text-h5")
    with ui.card().classes("w-full").mark("lista-robos"):
        if not robots:
            ui.label("Nenhum robô nas suas áreas ainda.")
        for robot in robots:
            with ui.row().classes("w-full items-center justify-between").mark(f"robo-{robot.slug}"):
                with ui.column().classes("gap-0"):
                    ui.label(robot.title).classes("text-weight-medium")
                    published = (
                        f"versão publicada {robot.current_version_no}"
                        if robot.current_version_no
                        else "sem versão publicada"
                    )
                    ui.label(
                        f"{robot.slug} · {ROBOT_STATUS_LABELS[robot.status]} · {published} · "
                        f"{viewer.area_label(robot.area_id)}"
                    ).classes("text-caption")
                ui.button(
                    "Abrir",
                    on_click=lambda r=robot: ui.navigate.to(f"/robos/{r.robot_id}"),
                ).props("flat").mark(f"abrir-{robot.slug}")
    _create_version_card(viewer, robots)


def _create_version_card(viewer: Viewer, robots: list[RobotInfo]) -> None:
    principal = viewer.principal
    areas = creator_areas(principal, [r.area_id for r in robots])
    anywhere = can_create_anywhere(principal)
    if not areas and not anywhere:
        return
    with ui.card().classes("w-full").mark("nova-versao"):
        ui.label("Nova versão de robô").classes("text-h6")
        ui.label(
            "Cole o manifesto do robô em JSON. Se já existir um robô com o mesmo nome técnico "
            "na área, a versão entra como a próxima dele."
        ).classes("text-caption")
        options = {str(area): viewer.area_label(area) for area in areas}
        area_select = (
            ui.select(options, label="Área", value=next(iter(options), None))
            .classes("w-96")
            .mark("area")
        )
        other_area = (
            ui.input("Ou o identificador de outra área").classes("w-96").mark("outra-area")
            if anywhere
            else None
        )
        text = (
            ui.textarea("Manifesto (JSON)")
            .classes("w-full")
            .props("rows=12")
            .mark("manifesto-json")
        )
        errors = ui.column().classes("text-negative").mark("erros-manifesto")

        async def create() -> None:
            errors.clear()
            raw_area = (other_area.value or "").strip() if other_area is not None else ""
            area_id = _parse_uuid(raw_area) if raw_area else _parse_uuid(area_select.value or "")
            if area_id is None:
                ui.notify("Escolha uma área válida.", type="warning")
                return
            backend = await viewer.backend()
            try:
                info = await backend.create_version(area_id, text.value or "")
            except InvalidInput as exc:
                with errors:
                    ui.label(exc.message[:1].upper() + exc.message[1:])
                    for detail in exc.errors:
                        ui.label(detail).classes("text-caption")
                return
            ui.notify(f"Versão {info.version_no} criada.", type="positive")
            ui.navigate.to(f"/robos/{info.robot_id}?versao={info.version_no}")

        ui.button("Criar versão", on_click=lambda: run_action(create)).mark("criar-versao")


async def _robot(viewer: Viewer, robot_id: str, versao: str) -> None:
    robot_uuid = _parse_uuid(robot_id)
    backend = await viewer.backend()
    robots = await backend.list_robots() if robot_uuid is not None else []
    robot = next((r for r in robots if r.robot_id == robot_uuid), None)
    if robot is None or robot_uuid is None:
        _not_found("Robô não encontrado.")
        return
    versions = await backend.list_versions(robot_uuid)
    ui.label(robot.title).classes("text-h5").mark("titulo-robo")
    ui.label(
        f"{robot.slug} · {ROBOT_STATUS_LABELS[robot.status]} · {viewer.area_label(robot.area_id)}"
    ).classes("text-caption")
    if not versions:
        _not_found("Este robô ainda não tem versões.")
        return
    wanted = _parse_int(versao) if versao else versions[-1].version_no
    selected = next((v for v in versions if v.version_no == wanted), None)
    if selected is None:
        _not_found("Versão não encontrada.")
        return
    _versions_card(robot, versions, selected)
    a1 = await load_a1(viewer, robot, selected)
    _approval_card(viewer, robot, selected, a1)
    if a1 is not None:
        a1_card(viewer, robot, selected, a1)
    base = _diff_base(robot, versions, selected)
    _diff_card(base, selected)
    _steps_card(selected.manifest)
    _io_card(selected.manifest)


def _versions_card(
    robot: RobotInfo, versions: list[RobotVersionInfo], selected: RobotVersionInfo
) -> None:
    with ui.card().classes("w-full").mark("versoes"):
        ui.label("Versões").classes("text-h6")
        for version in reversed(versions):
            current = version.version_no == selected.version_no
            with (
                ui.row()
                .classes("w-full items-center justify-between")
                .mark(f"versao-{version.version_no}")
            ):
                ui.label(
                    f"Versão {version.version_no} · {VERSION_STATUS_LABELS[version.status]} · "
                    f"criada por {version.created_by} em {format_datetime(version.created_at)}"
                    + (f" · aprovada por {version.approved_by}" if version.approved_by else "")
                    + _approvals_text(version)
                ).classes("text-weight-bold" if current else "")
                if not current:
                    ui.button(
                        "Ver",
                        on_click=lambda n=version.version_no: ui.navigate.to(
                            f"/robos/{robot.robot_id}?versao={n}"
                        ),
                    ).props("flat dense").mark(f"ver-versao-{version.version_no}")


def _counts_approvals(version: RobotVersionInfo) -> bool:
    """Se a contagem ("1 de 2 aprovações") aparece: só quando a versão precisa de mais de
    uma aprovação (o certificado A1). Com uma só, a aprovação já publica."""
    return version.status is VersionStatus.VALIDATED and version.approvals_required > 1


def _approvals_text(version: RobotVersionInfo) -> str:
    """O trecho "1 de 2 aprovações" de uma versão validada que ainda espera aprovação."""
    if not _counts_approvals(version):
        return ""
    return f" · {len(version.approvals)} de {version.approvals_required} aprovações"


def _approval_card(
    viewer: Viewer, robot: RobotInfo, version: RobotVersionInfo, a1: A1Context | None = None
) -> None:
    principal = viewer.principal
    number = version.version_no
    with ui.card().classes("w-full").mark("aprovacao"):
        with ui.row().classes("items-center gap-2"):
            ui.label(f"Versão {number}").classes("text-h6")
            ui.badge(VERSION_STATUS_LABELS[version.status]).mark("situacao-versao")
            effect_badge(version.manifest.effect())
        ui.label(f"Impressão digital: {version.fingerprint[:16]}…").classes("text-caption")
        if version.violations:
            with ui.column().classes("w-full bg-red-1 p-2 rounded").mark("violacoes"):
                ui.label("Problemas encontrados pelo portão de política:").classes(
                    "text-negative text-weight-medium"
                )
                for message in version.violations:
                    ui.label(f"• {message}").classes("text-negative")

        async def validate() -> None:
            backend = await viewer.backend()
            await backend.validate_version(robot.robot_id, number)
            ui.notify(f"Versão {number} marcada como validada.", type="positive")
            ui.navigate.to(f"/robos/{robot.robot_id}?versao={number}")

        async def publish() -> None:
            backend = await viewer.backend()
            info = await backend.publish_version(robot.robot_id, number)
            if info.status is VersionStatus.PUBLISHED:
                ui.notify(f"Versão {number} publicada.", type="positive")
            else:
                ui.notify(
                    "Aprovação registrada; falta a aprovação de outra pessoa.", type="positive"
                )
            ui.navigate.to(f"/robos/{robot.robot_id}?versao={number}")

        if version.status is VersionStatus.DRAFT:
            reason = validate_block_reason(principal, robot.area_id, version)
            if reason is None:
                ui.label(
                    "Depois de conferir os passos, marque a versão como validada. "
                    "Outra pessoa, com papel de aprovador, poderá então publicá-la."
                ).classes("text-caption")
                ui.button("Marcar como validada", on_click=lambda: run_action(validate)).mark(
                    "validar"
                )
            else:
                ui.label(reason).classes("text-warning").mark("motivo-validar")
        elif version.status is VersionStatus.VALIDATED:
            required = version.approvals_required
            if _counts_approvals(version):
                ui.label(f"{len(version.approvals)} de {required} aprovações").mark(
                    "contagem-aprovacoes"
                )
            if version.approvals:
                ui.label(f"Aprovada até agora por: {', '.join(version.approvals)}").classes(
                    "text-caption"
                ).mark("aprovadores")
            custodians = a1.custodians if a1 is not None else set()
            problems = a1.problems if a1 is not None else []
            reason = publish_block_reason(principal, robot.area_id, version, custodians, problems)
            if reason is None:
                last = len(version.approvals) + 1 >= required
                ui.label(
                    "Ao publicar, o robô vira ferramenta MCP e endpoint REST para quem tem acesso à área."
                    if last
                    else "Esta versão precisa da aprovação de mais uma pessoa depois da sua."
                ).classes("text-caption")
                ui.button(
                    "Publicar" if last else "Aprovar",
                    on_click=lambda: run_action(publish),
                    color="positive",
                ).mark("publicar")
            else:
                ui.label(reason).classes("text-warning").mark("motivo-publicar")


def _diff_base(
    robot: RobotInfo, versions: list[RobotVersionInfo], selected: RobotVersionInfo
) -> RobotVersionInfo | None:
    """Com o que comparar: a versão publicada, ou a anterior se a publicada for esta."""
    by_number = {v.version_no: v for v in versions}
    published = by_number.get(robot.current_version_no or 0)
    if published is not None and published.version_no != selected.version_no:
        return published
    return by_number.get(selected.version_no - 1)


def _diff_card(base: RobotVersionInfo | None, selected: RobotVersionInfo) -> None:
    with ui.card().classes("w-full").mark("diferencas"):
        if base is None:
            ui.label("Mudanças").classes("text-h6")
            ui.label("Primeira versão: não há versão anterior para comparar.")
            return
        situation = " (publicada)" if base.status is VersionStatus.PUBLISHED else ""
        ui.label(f"Mudanças em relação à versão {base.version_no}{situation}").classes("text-h6")
        diff = describe.diff_manifests(base.manifest, selected.manifest)
        if diff.empty:
            ui.label("Nenhuma mudança.")
            return
        if diff.raises_effect:
            ui.label(
                "Atenção: esta versão grava mais coisas no sistema de destino do que a anterior."
            ).classes("text-warning text-weight-medium").mark("efeito-aumentou")
        sections = (
            ("Passos novos", diff.added, "passo-novo"),
            ("Passos removidos", diff.removed, "passo-removido"),
            ("Passos alterados", diff.changed, "passo-alterado"),
        )
        for title, changes, marker in sections:
            if not changes:
                continue
            ui.label(title).classes("text-subtitle1")
            for change in changes:
                with ui.column().classes("gap-0 pl-2").mark(f"{marker}-{change.step_id}"):
                    with ui.row().classes("items-center gap-2"):
                        ui.label(f"{change.number}. {change.text}")
                        effect_badge(change.effect)
                    for item in change.fields:
                        ui.label(f"{item.label}: {item.before} → {item.after}").classes(
                            "text-caption pl-4"
                        )
        if diff.info:
            ui.label("Outras mudanças").classes("text-subtitle1")
            for item in diff.info:
                ui.label(f"{item.label}: {item.before} → {item.after}").classes("text-caption")


def _steps_card(manifest: Manifest) -> None:
    with ui.card().classes("w-full").mark("passos"):
        ui.label("Passos").classes("text-h6")
        ui.label(manifest.description).classes("text-caption")
        for line in describe.describe_steps(manifest):
            with (
                ui.row()
                .classes("items-center gap-2 no-wrap")
                .style(f"margin-left: {line.depth * 24}px") as row
            ):
                if line.kind == "otherwise":
                    ui.label(line.text).classes("text-italic")
                    continue
                ui.label(f"{line.number}. {line.text}")
                if line.effect is not Effect.READ:
                    effect_badge(line.effect)
            if line.step_id:
                row.mark(f"passo-{line.step_id}")


def _io_card(manifest: Manifest) -> None:
    with ui.card().classes("w-full").mark("entradas-saidas"):
        ui.label("Entradas").classes("text-h6")
        inputs = describe.describe_inputs(manifest)
        if inputs:
            rows: list[list[str]] = []
            for item in inputs:
                rows.append([item.title, item.type, item.required, item.description or "—"])
                rows += [
                    [f"  ↳ {sub.title}", sub.type, sub.required, sub.description or "—"]
                    for sub in item.item_fields
                ]
            simple_table(["Entrada", "Tipo", "Obrigatória?", "Descrição"], rows, marker="entradas")
        else:
            ui.label("Sem entradas.")
        ui.label("Saídas").classes("text-h6")
        outputs = describe.describe_outputs(manifest)
        if outputs:
            simple_table(
                ["Saída", "Tipo", "Vem de"],
                [[o.title, o.type, o.source] for o in outputs],
                marker="saidas",
            )
        else:
            ui.label("Sem saídas.")
        credentials = describe.describe_credentials(manifest)
        if credentials:
            ui.label("Credenciais do cofre").classes("text-h6")
            simple_table(
                ["Credencial", "Tipo", "Usada em"],
                [[c.name, c.kind, c.origin] for c in credentials],
                marker="credenciais",
            )
        ui.label(f"Domínios permitidos: {', '.join(manifest.allowed_domains)}").classes(
            "text-caption"
        )
        checks = describe.describe_postconditions(manifest)
        if checks:
            ui.label("Ao final, confere que: " + "; ".join(checks)).classes("text-caption")


# ---------------------------------------------------------------------------
# Executar
# ---------------------------------------------------------------------------


async def _execute(viewer: Viewer, slug: str) -> None:
    backend = await viewer.backend()
    summary = await backend.get_catalog_robot(slug)
    if summary is None:
        _not_found("Robô não encontrado ou não publicado.")
        return
    with ui.row().classes("items-center gap-2"):
        ui.label(f"Executar: {summary.title}").classes("text-h5").mark("titulo-executar")
        effect_badge(summary.effect)
    ui.label(summary.description)
    if not can_run(viewer.principal, summary.area_id):
        ui.label("Você não tem permissão para executar este robô.").classes("text-warning")
        return
    confirm: ui.checkbox | None = None
    if summary.effect is Effect.IRREVERSIBLE:
        ui.label(
            "Este robô faz uma ação irreversível no sistema de destino (por exemplo, emitir um documento)."
        ).classes("text-negative")
    elif summary.effect is Effect.WRITE:
        ui.label("Este robô grava dados no sistema de destino.").classes("text-warning")
    with ui.card().classes("w-full"):
        form = SchemaForm(summary.input_schema)
        form.build()
        if summary.effect is Effect.IRREVERSIBLE:
            confirm = ui.checkbox(
                "Entendo que esta execução faz uma ação irreversível no sistema de destino."
            ).mark("confirmar-irreversivel")
        button = ui.button("Executar").mark("executar")
    status_area = ui.column().classes("w-full").mark("area-execucao")
    attempt = {"key": new_id().hex}

    async def submit() -> None:
        if confirm is not None and not confirm.value:
            ui.notify(
                "Confirme que entende o efeito irreversível antes de executar.", type="warning"
            )
            return
        values = form.submit()
        if values is None:
            ui.notify("Corrija os campos destacados.", type="warning")
            return
        button.disable()
        try:
            current = await viewer.backend()
            status = await current.start_run(summary.slug, values, attempt["key"])
        except InvalidInput as exc:
            form.show_server_errors(exc.errors)
            notify_error(exc)
            attempt["key"] = new_id().hex
            return
        except NotAuthenticated:
            raise
        except BackendUnavailable as exc:
            # Mesma chave na próxima tentativa: se o pedido chegou, não duplica a execução.
            notify_error(exc)
            return
        except (ContractError, BackendError) as exc:
            notify_error(exc)
            attempt["key"] = new_id().hex
            return
        finally:
            button.enable()
        attempt["key"] = new_id().hex
        ui.notify("Execução iniciada.", type="positive")
        status_area.clear()
        with status_area:
            RunPanel(viewer, status).build()

    button.on_click(lambda: run_action(submit))


# ---------------------------------------------------------------------------
# Execuções
# ---------------------------------------------------------------------------

_INDETERMINATE_HELP = (
    "O robô pode ter gravado algo no sistema de destino antes de parar. Confira lá antes "
    "de tentar de novo: o RaPA nunca repete sozinho um passo que grava."
)


def _format_output(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


class RunPanel:
    """Estado de uma execução, atualizado periodicamente até terminar.

    ``on_finished`` é chamado uma vez quando a execução termina enquanto a página está
    aberta (a tela da execução mostra as evidências nessa hora).
    """

    def __init__(
        self,
        viewer: Viewer,
        status: RunStatus,
        on_finished: Callable[[RunStatus], Awaitable[None]] | None = None,
    ) -> None:
        self.viewer = viewer
        self.status = status
        self.on_finished = on_finished
        self.container: ui.column | None = None
        self.notice: ui.label | None = None
        self.timer: ui.timer | None = None

    def build(self) -> None:
        self.container = ui.column().classes("w-full gap-2").mark(f"execucao-{self.status.run_id}")
        self.notice = ui.label("").classes("text-caption text-warning").mark("aviso-atualizacao")
        self.notice.visible = False
        self._render()
        if not self.status.state.finished:
            self.timer = ui.timer(self.viewer.ctx.settings.poll_interval_s, self.poll)

    def _render(self) -> None:
        if self.container is None:
            return
        status = self.status
        self.container.clear()
        with self.container, ui.card().classes("w-full"):
            with ui.row().classes("items-center gap-2"):
                ui.label(f"Execução de {status.robot_slug}").classes("text-h6")
                state_badge(status.state)
            ui.label(f"Número: {status.run_id}").classes("text-caption").mark("numero-execucao")
            details = [
                ["Versão do robô", str(status.version_no)],
                ["Origem", TRIGGER_LABELS[status.trigger]],
                ["Criada em", format_datetime(status.created_at)],
                ["Começou em", format_datetime(status.started_at)],
                ["Terminou em", format_datetime(status.finished_at)],
                ["Passos concluídos", str(status.steps_done)],
            ]
            if status.queue_position is not None and status.state is RunState.QUEUED:
                details.append(["Posição na fila", str(status.queue_position)])
            simple_table(["", ""], details)
            if status.state is RunState.INDETERMINATE:
                ui.label(_INDETERMINATE_HELP).classes("text-purple-9").mark("indeterminada")
            if status.error_message or status.error_code:
                with ui.column().classes("w-full bg-red-1 p-2 rounded").mark("erro-execucao"):
                    ui.label(status.error_message or "A execução falhou.").classes("text-negative")
                    if status.error_code:
                        ui.label(f"Código: {status.error_code}").classes("text-caption")
            if status.outputs:
                ui.label("Resultado").classes("text-subtitle1")
                with ui.column().classes("w-full gap-1").mark("saidas-execucao"):
                    for name, value in status.outputs.items():
                        if (
                            isinstance(value, list)
                            and value
                            and all(isinstance(row, dict) for row in value)
                        ):
                            ui.label(name).classes("text-weight-medium")
                            headers = list(value[0])
                            simple_table(
                                headers,
                                [
                                    [_format_output(row.get(h, "")) for h in headers]
                                    for row in value[:200]
                                ],
                            )
                        else:
                            ui.label(f"{name}: {_format_output(value)}")

    async def poll(self) -> None:
        try:
            backend = await self.viewer.backend()
            status = await backend.get_run(self.status.run_id)
        except NotAuthenticated:
            self._stop()
            self._show_notice("Sua sessão expirou; entre de novo para acompanhar a execução.")
            return
        except ContractError, BackendError:
            self._show_notice("Não foi possível atualizar agora; tentando de novo.")
            return
        if status is None:
            self._stop()
            self._show_notice("A execução não foi encontrada.")
            return
        self._show_notice(None)
        if status != self.status:
            self.status = status
            self._render()
        if status.state.finished:
            self._stop()
            if self.on_finished is not None:
                finished, self.on_finished = self.on_finished, None
                await finished(status)

    def _stop(self) -> None:
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None

    def _show_notice(self, text: str | None) -> None:
        if self.notice is not None:
            self.notice.text = text or ""
            self.notice.visible = bool(text)


async def _runs(viewer: Viewer) -> None:
    ui.label("Execuções").classes("text-h5")
    ui.label(
        "As execuções mais recentes dos robôs das suas áreas, iniciadas por aqui, pelo MCP "
        "ou pela API, das mais novas às mais antigas."
    ).classes("text-caption")
    with ui.row().classes("items-center gap-2"):
        number = ui.input("Abrir execução pelo número").classes("w-96").mark("numero-busca")

        def open_run() -> None:
            run_id = _parse_uuid((number.value or "").strip())
            if run_id is None:
                ui.notify("Número de execução inválido.", type="warning")
                return
            ui.navigate.to(f"/execucoes/{run_id}")

        ui.button("Abrir", on_click=open_run).mark("abrir-execucao")
    backend = await viewer.backend()
    recent = await backend.list_runs(RECENT_RUNS)
    titles = {summary.slug: summary.title for summary in await backend.list_catalog()}
    with ui.card().classes("w-full").mark("lista-execucoes"):
        if not recent:
            ui.label("Nenhuma execução recente.")
        for status in recent:
            with (
                ui.row()
                .classes("w-full items-center justify-between")
                .mark(f"linha-{status.run_id}")
            ):
                with ui.row().classes("items-center gap-2"):
                    ui.label(titles.get(status.robot_slug, status.robot_slug)).classes(
                        "text-weight-medium"
                    )
                    state_badge(status.state)
                    ui.label(format_datetime(status.created_at)).classes("text-caption")
                ui.button(
                    "Detalhes",
                    on_click=lambda r=status.run_id: ui.navigate.to(f"/execucoes/{r}"),
                ).props("flat").mark(f"detalhes-{status.run_id}")


async def _run_manifest(viewer: Viewer, status: RunStatus) -> Manifest | None:
    """O manifesto da versão da execução (``None`` se o robô não é visível)."""
    backend = await viewer.backend()
    robot = next((r for r in await backend.list_robots() if r.slug == status.robot_slug), None)
    if robot is None:
        return None
    version = await backend.get_version(robot.robot_id, status.version_no)
    return version.manifest if version is not None else None


async def _run_detail(viewer: Viewer, run_id: str) -> None:
    run_uuid = _parse_uuid(run_id)
    backend = await viewer.backend()
    status = await backend.get_run(run_uuid) if run_uuid is not None else None
    if status is None:
        _not_found("Execução não encontrada.")
        return
    ui.label("Execução").classes("text-h5")
    panel = RunPanel(viewer, status)
    panel.build()
    # As evidências (telas, vídeo da falha, arquivos) quando a execução termina.
    evidence = ui.column().classes("w-full")

    async def show_evidence(final: RunStatus) -> None:
        evidence.clear()
        with evidence:
            await evidence_card(viewer, final)

    if status.state.finished:
        await show_evidence(status)
    else:
        panel.on_finished = show_evidence
    manifest = await _run_manifest(viewer, status) if can_repair_code(status) else None
    if can_repair(status, manifest):
        with ui.row().classes("items-center gap-2"):
            ui.button(
                "Consertar este passo",
                on_click=lambda: ui.navigate.to(f"/execucoes/{status.run_id}/conserto"),
            ).mark("consertar")
            ui.label(
                "O elemento de um passo não foi encontrado: escolha o elemento novo entre os "
                "da página na hora da falha."
            ).classes("text-caption")
