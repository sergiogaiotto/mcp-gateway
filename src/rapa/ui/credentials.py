"""Tela "Credenciais", o cartão do certificado A1 na tela do robô e o aviso de vencimento.

A interface continua fina: quem registra, renova, revoga e autoriza, e o que falta no
certificado, o plano de controle decide (:class:`rapa.control.services.credentials.PgCredentials`).
Aqui só não aparecem os botões que o servidor vai recusar
(:mod:`rapa.ui.permissions`). O valor de uma credencial nunca passa pelo RaPA: a tela
registra só o caminho no cofre e os metadados. Todo texto vindo de dados entra por
``ui.label`` (escapado).
"""

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from uuid import UUID

from nicegui import ui

from rapa.core.contracts import (
    A1AuthorizationInfo,
    CredentialInfo,
    CredentialStatus,
    RobotInfo,
    RobotVersionInfo,
    uses_a1,
)
from rapa.core.cron import DEFAULT_TIMEZONE, load_zone
from rapa.core.robot import CredentialKind
from rapa.ui.backend import BackendError, BackendUnavailable, NotAuthenticated
from rapa.ui.context import Viewer
from rapa.ui.labels import CREDENTIAL_KIND_LABELS, format_datetime
from rapa.ui.layout import run_action, simple_table
from rapa.ui.permissions import (
    admin_areas,
    can_register_credential_anywhere,
    can_revoke_credential,
    is_custodian,
    sees_expiry_alerts,
)

__all__ = [
    "CREDENTIAL_STATUS_LABELS",
    "A1Context",
    "a1_card",
    "credentials_page",
    "expiry_banner",
    "load_a1",
    "parse_validity",
]

logger = logging.getLogger(__name__)

CREDENTIAL_STATUS_LABELS: dict[CredentialStatus, str] = {
    CredentialStatus.ACTIVE: "Ativa",
    CredentialStatus.EXPIRING: "Vence em breve",
    CredentialStatus.EXPIRED: "Vencida",
    CredentialStatus.REVOKED: "Revogada",
}
_STATUS_COLORS: dict[CredentialStatus, str] = {
    CredentialStatus.ACTIVE: "positive",
    CredentialStatus.EXPIRING: "warning",
    CredentialStatus.EXPIRED: "negative",
    CredentialStatus.REVOKED: "grey",
}
_ZONE = load_zone(DEFAULT_TIMEZONE)
_BR_DATE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")


def parse_validity(text: str) -> datetime | None:
    """A validade digitada (dd/mm/aaaa ou aaaa-mm-dd) como o fim do dia em Brasília."""
    cleaned = (text or "").strip()
    if not cleaned:
        return None
    try:
        if match := _BR_DATE.fullmatch(cleaned):
            day = date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
        else:
            day = date.fromisoformat(cleaned)
    except ValueError:
        return None
    return datetime.combine(day, time(23, 59, 59), tzinfo=_ZONE)


def _validity(info: CredentialInfo) -> str:
    if info.kind is not CredentialKind.PFX_A1:
        return "—"
    if info.not_after is None:
        return "sem validade registrada"
    return format_datetime(info.not_after)


def _status_badge(info: CredentialInfo) -> None:
    ui.badge(CREDENTIAL_STATUS_LABELS[info.status], color=_STATUS_COLORS[info.status]).mark(
        f"situacao-credencial-{info.name}"
    )


# ---------------------------------------------------------------------------
# Aviso de vencimento (no topo das páginas)
# ---------------------------------------------------------------------------


async def expiry_banner(viewer: Viewer) -> None:
    """O aviso dos certificados A1 que vencem em breve, para custodiantes e administradores.

    Nunca impede a página de abrir: sem o serviço, ou com o serviço fora do ar, o aviso
    simplesmente não aparece.
    """
    if not sees_expiry_alerts(viewer.principal):
        return
    try:
        backend = await viewer.backend()
        expiring = await backend.expiring_credentials()
    except NotAuthenticated:
        raise
    except BackendError:
        return
    except Exception as exc:  # o aviso é só um aviso: a página segue
        logger.warning("aviso de vencimento indisponível: %s", type(exc).__name__)
        return
    # O servidor já filtra pelas áreas que a pessoa vê.
    mine = list(expiring)
    if not mine:
        return
    expired = sum(1 for c in mine if c.status is CredentialStatus.EXPIRED)
    with ui.card().classes("w-full bg-amber-1").mark("aviso-vencimento"):
        if expired:
            ui.label(
                f"{expired} certificado(s) A1 vencido(s): os robôs que dependem deles não rodam."
            ).classes("text-negative text-weight-medium")
        soon = len(mine) - expired
        if soon:
            ui.label(f"{soon} certificado(s) A1 vence(m) em breve: renove no cofre a tempo.")
        ui.link("Ver as credenciais", "/credenciais").mark("ver-vencimentos")


# ---------------------------------------------------------------------------
# Tela Credenciais
# ---------------------------------------------------------------------------


async def credentials_page(viewer: Viewer) -> None:
    ui.label("Credenciais").classes("text-h5")
    ui.label(
        "O RaPA guarda só a referência ao cofre (o caminho) e os metadados; o valor da senha "
        "ou do certificado nunca passa por aqui. Um certificado digital A1 tem custodiante, "
        "validade e, para cada robô, a autorização do custodiante."
    ).classes("text-caption")
    backend = await viewer.backend()
    items = await backend.list_credentials()
    expiring = await backend.expiring_credentials()
    _expiring_card(viewer, expiring)
    _list_card(viewer, items)
    # As áreas conhecidas: as dos papéis, das credenciais e dos robôs da empresa (os nomes
    # configurados valem para todas as empresas do servidor e não entram na lista).
    known = [
        *(c.area_id for c in items),
        *(r.area_id for r in await backend.list_robots()),
    ]
    areas = admin_areas(viewer.principal, known)
    # Quem administra a empresa inteira registra também numa área que a tela ainda não
    # conhece (sem robôs nem credenciais), pelo identificador, como na tela Gravar.
    anywhere = can_register_credential_anywhere(viewer.principal)
    if areas or anywhere:
        _register_card(viewer, areas, anywhere=anywhere)


def _expiring_card(viewer: Viewer, expiring: Sequence[CredentialInfo]) -> None:
    with ui.card().classes("w-full").mark("vencimentos"):
        ui.label("Vencimentos").classes("text-h6")
        if not expiring:
            ui.label("Nenhum certificado A1 vence em breve.").mark("sem-vencimentos")
            return
        simple_table(
            ["Certificado", "Área", "Custodiante", "Validade", "Situação"],
            [
                [
                    c.name,
                    viewer.area_label(c.area_id),
                    c.custodian_name or c.custodian_subject or "—",
                    _validity(c),
                    CREDENTIAL_STATUS_LABELS[c.status],
                ]
                for c in expiring
            ],
            marker="tabela-vencimentos",
        )


def _list_card(viewer: Viewer, items: Sequence[CredentialInfo]) -> None:
    with ui.card().classes("w-full").mark("lista-credenciais"):
        ui.label("Credenciais registradas").classes("text-h6")
        if not items:
            ui.label("Nenhuma credencial registrada nas suas áreas.").mark("sem-credenciais")
            return
        for info in items:
            _credential_row(viewer, info)


def _credential_row(viewer: Viewer, info: CredentialInfo) -> None:
    principal = viewer.principal
    with ui.column().classes("w-full gap-1 border rounded p-2").mark(f"credencial-{info.name}"):
        with ui.row().classes("items-center gap-2"):
            ui.label(info.name).classes("text-weight-medium")
            ui.badge(CREDENTIAL_KIND_LABELS[info.kind]).props("outline")
            _status_badge(info)
        details = [
            f"área: {viewer.area_label(info.area_id)}",
            f"origem: {info.origin}",
            f"custodiante: {info.custodian_name or info.custodian_subject or '—'}",
            f"validade: {_validity(info)}",
        ]
        if info.fingerprint_sha256:
            details.append(f"impressão: {info.fingerprint_sha256[:16]}…")
        if info.vault_path:
            details.append(f"cofre: {info.vault_path}")
        if info.revoked_at:
            details.append(f"revogada por {info.revoked_by} em {format_datetime(info.revoked_at)}")
        ui.label(" · ".join(details)).classes("text-caption break-words")
        custodian = is_custodian(principal, info)
        revocable = can_revoke_credential(principal, info)
        if not (custodian or revocable) or info.status is CredentialStatus.REVOKED:
            return
        with ui.row().classes("items-end gap-2"):
            if custodian and info.kind is CredentialKind.PFX_A1:
                validity = (
                    ui.input("Nova validade (dd/mm/aaaa)")
                    .classes("w-48")
                    .mark(f"validade-{info.name}")
                )
                fingerprint = (
                    ui.input("Impressão SHA-256 do certificado novo")
                    .classes("w-96")
                    .mark(f"impressao-{info.name}")
                )

                async def renew() -> None:
                    when = parse_validity(validity.value or "")
                    if when is None:
                        ui.notify("Informe a nova validade (dd/mm/aaaa).", type="warning")
                        return
                    current = await viewer.backend()
                    await current.renew_credential(
                        info.credential_id, when, (fingerprint.value or "").strip() or None
                    )
                    ui.notify("Validade renovada.", type="positive")
                    ui.navigate.to("/credenciais")

                ui.button("Renovar", on_click=lambda: run_action(renew)).props("flat").mark(
                    f"renovar-{info.name}"
                )
            if revocable:

                async def revoke() -> None:
                    current = await viewer.backend()
                    await current.revoke_credential(info.credential_id)
                    ui.notify("Credencial revogada.", type="positive")
                    ui.navigate.to("/credenciais")

                ui.button("Revogar", color="negative", on_click=lambda: run_action(revoke)).props(
                    "flat"
                ).mark(f"revogar-{info.name}")


def _parse_area(text: str) -> UUID | None:
    try:
        return UUID(text.strip())
    except ValueError:
        return None


def _register_card(viewer: Viewer, areas: Sequence[UUID], *, anywhere: bool = False) -> None:
    with ui.card().classes("w-full").mark("registrar-credencial"):
        ui.label("Registrar credencial").classes("text-h6")
        ui.label(
            "Guarde o valor no cofre antes; aqui vai só o caminho. O certificado A1 precisa "
            "de um custodiante (outra pessoa, com o papel de custodiante) e da validade."
        ).classes("text-caption")
        options = {str(a): viewer.area_label(a) for a in areas}
        area = (
            ui.select(options, label="Área", value=next(iter(options), None))
            .classes("w-96")
            .mark("area-credencial")
        )
        other_area = (
            ui.input("Ou o identificador de outra área")
            .classes("w-96")
            .mark("outra-area-credencial")
            if anywhere
            else None
        )
        name = ui.input("Nome (como no robô)").classes("w-96").mark("nome-credencial")
        kind = (
            ui.select(
                {k.value: label for k, label in CREDENTIAL_KIND_LABELS.items()},
                label="Tipo",
                value=CredentialKind.PFX_A1.value,
            )
            .classes("w-96")
            .mark("tipo-credencial")
        )
        origin = ui.input("Origem (https://portal)").classes("w-96").mark("origem-credencial")
        vault = ui.input("Caminho no cofre").classes("w-96").mark("cofre-credencial")
        custodian = (
            ui.input("Custodiante (usuário de login)")
            .classes("w-96")
            .mark("custodiante-credencial")
        )
        validity = ui.input("Validade (dd/mm/aaaa)").classes("w-48").mark("validade-credencial")
        fingerprint = (
            ui.input("Impressão SHA-256 (opcional)").classes("w-96").mark("impressao-credencial")
        )

        async def register() -> None:
            raw_validity = (validity.value or "").strip()
            when = parse_validity(raw_validity)
            if raw_validity and when is None:
                ui.notify("Validade inválida: use dd/mm/aaaa.", type="warning")
                return
            raw_area = (other_area.value or "").strip() if other_area is not None else ""
            area_id = _parse_area(raw_area or str(area.value or ""))
            if area_id is None:
                ui.notify("Escolha uma área válida.", type="warning")
                return
            current = await viewer.backend()
            await current.register_credential(
                area_id,
                name=(name.value or "").strip(),
                kind=CredentialKind(kind.value),
                origin=(origin.value or "").strip(),
                vault_path=(vault.value or "").strip(),
                custodian_subject=(custodian.value or "").strip() or None,
                not_after=when,
                fingerprint=(fingerprint.value or "").strip() or None,
            )
            ui.notify("Credencial registrada.", type="positive")
            ui.navigate.to("/credenciais")

        ui.button("Registrar", on_click=lambda: run_action(register)).mark("registrar")


# ---------------------------------------------------------------------------
# Cartão do certificado A1 na tela do robô
# ---------------------------------------------------------------------------


@dataclass
class A1Context:
    """O que a tela do robô precisa saber do certificado A1 dele.

    ``credentials`` são as registradas na área do robô com o nome de um certificado A1
    do manifesto; ``problems``, o que ainda falta para usá-lo (do servidor).
    """

    credentials: dict[str, CredentialInfo] = field(default_factory=dict)
    authorizations: list[A1AuthorizationInfo] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def custodians(self) -> set[str]:
        """Os custodiantes dos certificados A1 do robô (como o servidor conta: só os
        registrados como ``pfx_a1``; o custodiante de uma senha aprova normalmente)."""
        return {
            c.custodian_subject
            for c in self.credentials.values()
            if c.custodian_subject and c.kind is CredentialKind.PFX_A1
        }


async def load_a1(viewer: Viewer, robot: RobotInfo, version: RobotVersionInfo) -> A1Context | None:
    """As credenciais, as autorizações e o que falta (``None`` se a versão não usa A1)."""
    if not uses_a1(version.manifest):
        return None
    backend = await viewer.backend()
    names = {c.name for c in version.manifest.credentials if c.kind is CredentialKind.PFX_A1}
    try:
        registered = await backend.list_credentials(robot.area_id)
        approvals = await backend.version_approvals(robot.robot_id, version.version_no)
        authorizations = await backend.list_a1_authorizations(robot.robot_id)
    except BackendUnavailable as exc:
        # Sem as credenciais, a tela mostra o robô e diz por quê; o servidor recusa o resto.
        return A1Context(problems=[exc.message])
    return A1Context(
        credentials={c.name: c for c in registered if c.name in names},
        authorizations=authorizations,
        problems=list(approvals.problems),
    )


def a1_card(
    viewer: Viewer, robot: RobotInfo, version: RobotVersionInfo, context: A1Context
) -> None:
    principal = viewer.principal
    with ui.card().classes("w-full").mark("cartao-a1"):
        ui.label("Certificado digital A1").classes("text-h6")
        ui.label(
            "Este robô usa um certificado A1: precisa de duas aprovações, de pessoas que não "
            "são o custodiante, e roda só com a autorização do custodiante para este robô, "
            "com o certificado dentro da validade. O conserto assistido fica desligado."
        ).classes("text-caption")
        if context.problems:
            with ui.column().classes("w-full bg-amber-1 p-2 rounded").mark("a1-pendencias"):
                ui.label("O que falta para usar o certificado:").classes("text-weight-medium")
                for problem in context.problems:
                    ui.label(f"• {problem}")
        for ref in version.manifest.credentials:
            if ref.kind is not CredentialKind.PFX_A1:
                continue
            info = context.credentials.get(ref.name)
            with ui.column().classes("w-full gap-1 border rounded p-2").mark(f"a1-{ref.name}"):
                ui.label(f"{ref.name} · {ref.origin}").classes("text-weight-medium")
                if info is None:
                    ui.label("Não registrado nesta área.").classes("text-warning").mark(
                        f"a1-sem-registro-{ref.name}"
                    )
                    continue
                ui.label(
                    f"custodiante: {info.custodian_name or info.custodian_subject or '—'} · "
                    f"validade: {_validity(info)} · {CREDENTIAL_STATUS_LABELS[info.status]}"
                ).classes("text-caption")
                active = [
                    a
                    for a in context.authorizations
                    if a.credential_id == info.credential_id and a.revoked_at is None
                ]
                for authorization in active:
                    _authorization_row(viewer, robot, info, authorization)
                if not active and is_custodian(principal, info):

                    async def authorize(name: str = ref.name) -> None:
                        current = await viewer.backend()
                        await current.authorize_a1(robot.robot_id, name)
                        ui.notify("Robô autorizado neste portal.", type="positive")
                        ui.navigate.to(f"/robos/{robot.robot_id}?versao={version.version_no}")

                    ui.button(
                        "Autorizar este robô neste portal",
                        on_click=lambda action=authorize: run_action(action),
                    ).mark(f"autorizar-a1-{ref.name}")
                elif not active:
                    ui.label("Ainda sem a autorização do custodiante para este robô.").mark(
                        f"a1-sem-autorizacao-{ref.name}"
                    )


def _authorization_row(
    viewer: Viewer, robot: RobotInfo, info: CredentialInfo, authorization: A1AuthorizationInfo
) -> None:
    with (
        ui.row()
        .classes("w-full items-center justify-between")
        .mark(f"autorizacao-{authorization.authorization_id}")
    ):
        state = "vale" if authorization.current else "não vale (certificado trocado ou vencido)"
        ui.label(
            f"Autorizado por {authorization.authorized_by} em "
            f"{format_datetime(authorization.created_at)} · {state}"
        ).classes("text-caption")
        if not can_revoke_credential(viewer.principal, info):
            return

        async def revoke() -> None:
            current = await viewer.backend()
            await current.revoke_a1_authorization(robot.robot_id, authorization.authorization_id)
            ui.notify("Autorização revogada.", type="positive")
            ui.navigate.to(f"/robos/{robot.robot_id}")

        ui.button(
            "Revogar autorização", color="negative", on_click=lambda: run_action(revoke)
        ).props("flat").mark(f"revogar-autorizacao-{authorization.authorization_id}")
