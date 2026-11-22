"""Tela "Empresa": usuários e papéis, áreas e o consumo do mês.

As regras ficam no plano de controle (:mod:`rapa.control.services.company`); a tela só
esconde o que o servidor vai recusar. Quem administra a empresa vê e muda tudo; quem
administra uma área dá e tira só os papéis dela; quem audita vê o consumo. A empresa
congelada aparece no topo. Todo texto da página entra por ``ui.label`` (escapado).
"""

from uuid import UUID

from nicegui import ui

from rapa.core.contracts import (
    AreaInfo,
    CompanyInfo,
    CompanyStatus,
    CompanyUser,
    Principal,
    Role,
    RoleGrant,
    UsageReport,
)
from rapa.ui.context import Viewer
from rapa.ui.labels import COMPANY_STATUS_LABELS, ROLE_LABELS, TRIGGER_LABELS, format_datetime
from rapa.ui.layout import run_action, simple_table
from rapa.ui.permissions import (
    can_manage_company,
    can_manage_roles,
    can_open_company,
    can_see_usage,
    managed_areas,
)

__all__ = ["can_change_grant", "company_page"]

#: Valor do seletor de área que quer dizer "a empresa inteira".
_WHOLE_COMPANY = "empresa"


def can_change_grant(principal: Principal, grant: RoleGrant) -> bool:
    """Se a pessoa dá ou tira este papel (o servidor confere de novo)."""
    if grant.role is Role.PLATFORM_ADMIN:
        return False
    if can_manage_company(principal):
        return True
    if grant.area_id is None or grant.role is Role.COMPANY_ADMIN:
        return False
    managed = managed_areas(principal)
    return managed is None or grant.area_id in managed


def _grant_marker(user_id: UUID, grant: RoleGrant) -> str:
    return f"{user_id}-{grant.role.value}-{grant.area_id or _WHOLE_COMPANY}"


def _status_card(info: CompanyInfo) -> None:
    frozen = info.status is CompanyStatus.FROZEN
    with ui.card().classes(f"w-full {'bg-amber-1' if frozen else ''}").mark("empresa-situacao"):
        with ui.row().classes("items-center gap-2"):
            ui.label(info.name).classes("text-h6").mark("empresa-nome")
            ui.badge(COMPANY_STATUS_LABELS[info.status], color="orange-9" if frozen else "green-8")
        if frozen:
            ui.label(
                "A empresa está congelada: dá para ver tudo, mas nada novo pode ser criado nem "
                "executado até a plataforma descongelá-la."
            ).mark("empresa-congelada")
            if info.status_reason:
                ui.label(f"Motivo: {info.status_reason}").classes("text-caption")
            ui.label(f"Desde {format_datetime(info.status_changed_at)}").classes("text-caption")


def _area_name(viewer: Viewer, areas: dict[UUID, str], area_id: UUID | None) -> str:
    if area_id is None:
        return "Empresa inteira"
    return areas.get(area_id) or viewer.area_label(area_id)


def _user_card(viewer: Viewer, user: CompanyUser, areas: dict[UUID, str]) -> None:
    principal = viewer.principal
    mine = [g for g in user.roles if can_change_grant(principal, g)]
    with ui.card().classes("w-full").mark(f"usuario-{user.user_id}"):
        with ui.row().classes("w-full items-center justify-between"):
            with ui.column().classes("gap-0"):
                ui.label(user.display_name or user.subject).classes("text-weight-medium")
                ui.label(user.subject).classes("text-caption")
            ui.badge(
                "ativo" if user.active else "desativado",
                color="green-8" if user.active else "grey-7",
            )
        if not user.roles:
            ui.label("Sem papéis.").classes("text-caption").mark(f"sem-papeis-{user.user_id}")
        for grant in user.roles:
            with ui.row().classes("items-center gap-2"):
                ui.label(
                    f"{ROLE_LABELS[grant.role]} · {_area_name(viewer, areas, grant.area_id)}"
                ).mark(f"papel-{_grant_marker(user.user_id, grant)}")
                if grant in mine:

                    async def remove(g: RoleGrant = grant) -> None:
                        current = await viewer.backend()
                        await current.set_user_roles(user.user_id, [x for x in mine if x != g])
                        ui.notify("Papel tirado.", type="positive")
                        ui.navigate.to("/empresa")

                    ui.button(
                        "Tirar", on_click=lambda g=grant: run_action(lambda: remove(g))
                    ).props("flat dense color=negative").mark(
                        f"tirar-{_grant_marker(user.user_id, grant)}"
                    )
        _add_role_form(viewer, user, mine, areas)
        if can_manage_company(principal) and user.subject != principal.subject:
            if user.active:

                async def deactivate() -> None:
                    current = await viewer.backend()
                    await current.deactivate_user(user.user_id)
                    ui.notify("Usuário desativado.", type="positive")
                    ui.navigate.to("/empresa")

                ui.button("Desativar", on_click=lambda: run_action(deactivate)).props(
                    "flat color=negative"
                ).mark(f"desativar-{user.user_id}")
            else:

                async def reactivate() -> None:
                    current = await viewer.backend()
                    await current.reactivate_user(user.user_id)
                    ui.notify("Usuário reativado.", type="positive")
                    ui.navigate.to("/empresa")

                ui.button("Reativar", on_click=lambda: run_action(reactivate)).props("flat").mark(
                    f"reativar-{user.user_id}"
                )


def _add_role_form(
    viewer: Viewer, user: CompanyUser, mine: list[RoleGrant], areas: dict[UUID, str]
) -> None:
    principal = viewer.principal
    company_admin = can_manage_company(principal)
    roles = {
        role.value: ROLE_LABELS[role]
        for role in Role
        if role is not Role.PLATFORM_ADMIN and (company_admin or role is not Role.COMPANY_ADMIN)
    }
    managed = managed_areas(principal)
    places: dict[str, str] = {}
    if company_admin:
        places[_WHOLE_COMPANY] = "Empresa inteira"
    for area_id, name in areas.items():
        if company_admin or managed is None or area_id in managed:
            places[str(area_id)] = name
    if not places:
        return
    with ui.row().classes("items-center gap-2"):
        role_select = (
            ui.select(roles, label="Papel", value=Role.OPERATOR.value)
            .classes("w-56")
            .mark(f"papel-novo-{user.user_id}")
        )
        place_select = (
            ui.select(places, label="Onde", value=next(iter(places)))
            .classes("w-56")
            .mark(f"area-nova-{user.user_id}")
        )

        async def add() -> None:
            place = place_select.value or ""
            grant = RoleGrant(
                role=Role(role_select.value),
                area_id=None if place == _WHOLE_COMPANY else UUID(place),
            )
            if grant in mine:
                ui.notify("A pessoa já tem este papel.", type="warning")
                return
            current = await viewer.backend()
            await current.set_user_roles(user.user_id, [*mine, grant])
            ui.notify("Papel dado.", type="positive")
            ui.navigate.to("/empresa")

        ui.button("Dar papel", on_click=lambda: run_action(add)).mark(f"dar-papel-{user.user_id}")


def _areas_card(viewer: Viewer, areas: list[AreaInfo]) -> None:
    with ui.card().classes("w-full").mark("areas-empresa"):
        ui.label("Áreas").classes("text-h6")
        for area in areas:
            with ui.row().classes("items-center gap-2").mark(f"area-{area.area_id}"):
                name = (
                    ui.input("Nome", value=area.name)
                    .classes("w-72")
                    .mark(f"nome-area-{area.area_id}")
                )

                async def rename(area_id: UUID = area.area_id, field: ui.input = name) -> None:
                    current = await viewer.backend()
                    await current.rename_company_area(area_id, str(field.value or ""))
                    ui.notify("Área renomeada.", type="positive")
                    ui.navigate.to("/empresa")

                ui.button("Renomear", on_click=lambda r=rename: run_action(r)).props("flat").mark(
                    f"renomear-area-{area.area_id}"
                )
        with ui.row().classes("items-center gap-2"):
            new_name = ui.input("Nova área").classes("w-72").mark("nova-area")

            async def create() -> None:
                current = await viewer.backend()
                await current.create_company_area(str(new_name.value or ""))
                ui.notify("Área criada.", type="positive")
                ui.navigate.to("/empresa")

            ui.button("Criar área", on_click=lambda: run_action(create)).mark("criar-area")


def _usage_card(report: UsageReport) -> None:
    with ui.card().classes("w-full").mark("consumo"):
        ui.label(f"Consumo de {report.month}").classes("text-h6")
        ui.label(
            "Pelo mês em que cada execução foi pedida, no horário de Brasília. Minutos de "
            "navegador: do início ao fim de cada execução que começou."
        ).classes("text-caption")
        with ui.row().classes("items-center gap-2"):
            month = (
                ui.input("Mês (AAAA-MM)", value=report.month).classes("w-48").mark("mes-consumo")
            )
            ui.button(
                "Ver",
                on_click=lambda: ui.navigate.to(f"/empresa?mes={(month.value or '').strip()}"),
            ).props("flat").mark("ver-consumo")
        rows: list[list[str]] = []
        for area in report.areas:
            rows.append(
                [
                    area.area_name,
                    "todas",
                    str(area.runs),
                    f"{area.browser_minutes:.2f}",
                    f"{area.sessions} ({area.session_minutes:.2f} min)",
                    f"{area.batches} ({area.batch_rows} linhas)",
                ]
            )
            for usage in area.by_trigger:
                rows.append(
                    [
                        "",
                        TRIGGER_LABELS[usage.trigger],
                        str(usage.runs),
                        f"{usage.browser_minutes:.2f}",
                        "",
                        "",
                    ]
                )
        simple_table(
            ["Área", "Origem", "Execuções", "Minutos de navegador", "Sessões de gravação", "Lotes"],
            rows,
            marker="tabela-consumo",
        )
        ui.label(
            f"Total: {report.runs} execuções, {report.browser_minutes:.2f} min de navegador, "
            f"{report.sessions} sessões ({report.session_minutes:.2f} min), {report.batches} "
            f"lotes ({report.batch_rows} linhas)"
        ).classes("text-weight-medium").mark("consumo-total")


async def company_page(viewer: Viewer, month: str) -> None:
    """A tela Empresa (``/empresa``), com o consumo do mês ``month`` (ou o de agora)."""
    principal = viewer.principal
    ui.label("Empresa").classes("text-h5")
    backend = await viewer.backend()
    _status_card(await backend.get_company())
    if not can_open_company(principal):
        with ui.card().classes("w-full").mark("sem-permissao-empresa"):
            ui.label("Só quem administra a empresa ou uma área, ou quem audita, usa esta tela.")
        return
    if can_manage_roles(principal):
        areas = await backend.list_company_areas()
        names = {area.area_id: area.name for area in areas}
        ui.label("Usuários e papéis").classes("text-h6")
        if not can_manage_company(principal):
            ui.label("Você dá e tira só os papéis das áreas que administra.").classes(
                "text-caption"
            ).mark("so-minhas-areas")
        with ui.column().classes("w-full gap-3").mark("lista-usuarios"):
            for user in await backend.list_company_users():
                _user_card(viewer, user, names)
        if can_manage_company(principal):
            _areas_card(viewer, areas)
    if can_see_usage(principal):
        _usage_card(await backend.company_usage(month.strip() or None))
