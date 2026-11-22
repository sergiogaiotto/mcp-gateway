"""Tela Credenciais, o cartão do A1 na tela do robô e o aviso de vencimento.

Com o ``User`` simulado do NiceGUI, a plataforma em memória e um serviço de
credenciais em memória (as regras de verdade são testadas no plano de controle).
"""

import builtins
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from rapa.core.contracts import (
    A1AuthorizationInfo,
    ApprovalInfo,
    CredentialInfo,
    CredentialStatus,
    Forbidden,
    NotFound,
    Principal,
    RobotVersionInfo,
    Role,
    RunState,
    RunStatus,
    RunTrigger,
    VersionApprovals,
)
from rapa.core.ids import new_id
from rapa.core.robot import CredentialKind, Manifest
from rapa.fixtures.robots import emitir_protocolo
from rapa.ui.credentials import A1Context, parse_validity
from rapa.ui.permissions import can_revoke_credential, is_custodian, publish_block_reason
from rapa.ui.repair import can_repair
from tests.ui.conftest import BASE, HOST, World, login

pytestmark = pytest.mark.usefixtures("no_error_logs")

INPUTS = {"nome": "Maria", "cpf": "529.982.247-25", "uf": "SP"}
CERT = "certificado"


def a1_manifest() -> Manifest:
    data = emitir_protocolo(BASE, HOST).model_dump(mode="json")
    data["name"] = "protocolo_a1"
    data["credentials"] = [
        *data["credentials"],
        {"name": CERT, "kind": "pfx_a1", "origin": BASE},
    ]
    return Manifest.model_validate(data)


class MemoryCredentials:
    """Credenciais em memória, só para a interface (sem as regras do servidor)."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.items: dict[UUID, CredentialInfo] = {}
        self.authorizations: list[A1AuthorizationInfo] = []
        self.calls: list[tuple[str, str]] = []
        self.registered_areas: list[UUID] = []

    def add(
        self,
        area_id: UUID,
        custodian: str,
        days: float,
        *,
        name: str = CERT,
        kind: CredentialKind = CredentialKind.PFX_A1,
    ) -> CredentialInfo:
        now = datetime.now(UTC)
        info = CredentialInfo(
            credential_id=new_id(),
            area_id=area_id,
            name=name,
            kind=kind,
            origin=BASE,
            custodian_subject=custodian,
            not_after=now + timedelta(days=days),
            fingerprint_sha256="ab" * 32,
            status=CredentialStatus.EXPIRING if days < 30 else CredentialStatus.ACTIVE,
            created_at=now,
            updated_at=now,
            vault_path="cofre/empresa/a1",
        )
        self.items[info.credential_id] = info
        return info

    def _visible(self, principal: Principal, info: CredentialInfo) -> bool:
        return principal.company_id == self.world.company_a and bool(
            principal.roles_in(info.area_id)
        )

    async def register(
        self, principal: Principal, area_id: UUID, **fields: object
    ) -> CredentialInfo:
        self.calls.append(("registrar", principal.subject))
        self.registered_areas.append(area_id)
        info = self.add(area_id, str(fields["custodian_subject"]), 300, name=str(fields["name"]))
        return info

    async def renew(
        self,
        principal: Principal,
        credential_id: UUID,
        *,
        not_after: datetime,
        fingerprint: str | None = None,
    ) -> CredentialInfo:
        self.calls.append(("renovar", principal.subject))
        info = self.items[credential_id].model_copy(update={"not_after": not_after})
        self.items[credential_id] = info
        return info

    async def revoke(self, principal: Principal, credential_id: UUID) -> CredentialInfo:
        self.calls.append(("revogar", principal.subject))
        info = self.items[credential_id].model_copy(
            update={"status": CredentialStatus.REVOKED, "revoked_at": datetime.now(UTC)}
        )
        self.items[credential_id] = info
        return info

    async def list(
        self, principal: Principal, area_id: UUID | None = None
    ) -> builtins.list[CredentialInfo]:
        return [
            c
            for c in self.items.values()
            if self._visible(principal, c) and (area_id is None or c.area_id == area_id)
        ]

    async def expiring(
        self, principal: Principal, within_days: int | None = None
    ) -> builtins.list[CredentialInfo]:
        return [
            c
            for c in await self.list(principal)
            if c.status in (CredentialStatus.EXPIRING, CredentialStatus.EXPIRED)
        ]

    async def authorize(
        self, principal: Principal, robot_id: UUID, credential_name: str
    ) -> A1AuthorizationInfo:
        self.calls.append(("autorizar", principal.subject))
        info = next(c for c in self.items.values() if c.name == credential_name)
        if info.custodian_subject != principal.subject:
            raise Forbidden("só o custodiante deste certificado autoriza robôs a usá-lo")
        authorization = A1AuthorizationInfo(
            authorization_id=new_id(),
            robot_id=robot_id,
            area_id=info.area_id,
            credential_id=info.credential_id,
            credential_name=info.name,
            origin=info.origin,
            fingerprint_sha256=info.fingerprint_sha256,
            authorized_by=principal.subject,
            created_at=datetime.now(UTC),
            current=True,
        )
        self.authorizations.append(authorization)
        return authorization

    async def revoke_authorization(
        self, principal: Principal, authorization_id: UUID, *, robot_id: UUID | None = None
    ) -> A1AuthorizationInfo:
        raise NotFound("autorização não encontrada")

    async def list_authorizations(
        self, principal: Principal, robot_id: UUID
    ) -> builtins.list[A1AuthorizationInfo]:
        return [a for a in self.authorizations if a.robot_id == robot_id]

    async def approvals(
        self, principal: Principal, robot_id: UUID, version_no: int
    ) -> VersionApprovals:
        version = await self.world.platform.get_version(principal, robot_id, version_no)
        assert version is not None
        problems = (
            []
            if any(a.robot_id == robot_id for a in self.authorizations)
            else [f"falta a autorização do custodiante do certificado '{CERT}' para este robô"]
        )
        return VersionApprovals(
            robot_id=robot_id,
            version_no=version_no,
            status=version.status,
            required=version.approvals_required,
            approvals=[
                ApprovalInfo(approved_by=who, approved_at=datetime.now(UTC))
                for who in version.approvals
            ],
            missing=version.approvals_required - len(version.approvals),
            problems=problems,
        )


def _people(world: World) -> None:
    area = world.area_a

    def person(subject: str, *roles: Role) -> Principal:
        return Principal(
            subject=subject, company_id=world.company_a, area_roles={area: frozenset(roles)}
        )

    world.principals.update(
        {
            "administradora": person("adm@a", Role.AREA_ADMIN),
            "custodiante": person("guarda@a", Role.CREDENTIAL_CUSTODIAN, Role.APPROVER),
            "aprovadora": person("aprovadora@a", Role.APPROVER),
        }
    )


async def _a1_robot(world: World) -> RobotVersionInfo:
    creator = world.principals["criadora"]
    info = await world.platform.create_version(creator, world.area_a, a1_manifest())
    return await world.platform.mark_validated(creator, info.robot_id, 1)


async def test_credentials_page_by_role(world: World) -> None:
    _people(world)
    credentials = MemoryCredentials(world)
    credentials.add(world.area_a, "guarda@a", 10)
    world.credentials = credentials
    async with world.user() as user:
        # Quem opera vê a lista e os vencimentos, sem registrar, renovar ou revogar.
        await login(user, "operador", "/credenciais")
        await user.should_see(marker="lista-credenciais", retries=50)
        await user.should_see(marker=f"credencial-{CERT}")
        await user.should_see(marker="tabela-vencimentos")
        for marker in (
            "registrar-credencial",
            f"renovar-{CERT}",
            f"revogar-{CERT}",
            "aviso-vencimento",
        ):
            await user.should_not_see(marker=marker)
        user.find(marker="sair").click()
        await user.should_see(marker="pagina-login", retries=50)

        # Quem administra a área registra e revoga, mas não renova; vê o aviso no topo.
        await login(user, "administradora", "/credenciais")
        await user.should_see(marker="registrar-credencial", retries=50)
        await user.should_see(marker="aviso-vencimento")
        await user.should_see(marker=f"revogar-{CERT}")
        await user.should_not_see(marker=f"renovar-{CERT}")
        for marker, value in (
            ("nome-credencial", "certificado_novo"),
            ("origem-credencial", BASE),
            ("cofre-credencial", "cofre/novo"),
            ("custodiante-credencial", "guarda@a"),
            ("validade-credencial", "31/12/2030"),
        ):
            user.find(marker=marker).type(value)
        user.find(marker="registrar").click()
        await user.should_see(marker="credencial-certificado_novo", retries=50)
        assert ("registrar", "adm@a") in credentials.calls
        user.find(marker="sair").click()
        await user.should_see(marker="pagina-login", retries=50)

        # O custodiante renova; o aviso aparece também para ele, em qualquer tela.
        await login(user, "custodiante", "/catalogo")
        await user.should_see(marker="aviso-vencimento", retries=50)
        await user.open("/credenciais")
        await user.should_see(marker=f"renovar-{CERT}", retries=50)
        user.find(marker=f"validade-{CERT}").type("01/02/2031")
        user.find(marker=f"renovar-{CERT}").click()
        await user.should_see("Validade renovada.", retries=50)
        assert ("renovar", "guarda@a") in credentials.calls


async def test_robot_page_shows_the_a1_card_and_two_approvals(world: World) -> None:
    _people(world)
    credentials = MemoryCredentials(world)
    credentials.add(world.area_a, "guarda@a", 200)
    world.credentials = credentials
    version = await _a1_robot(world)
    path = f"/robos/{version.robot_id}"
    async with world.user() as user:
        # O custodiante autoriza, mas não aprova.
        await login(user, "custodiante", path)
        await user.should_see(marker="cartao-a1", retries=50)
        await user.should_see(marker="a1-pendencias")
        await user.should_see("0 de 2 aprovações")
        await user.should_see(marker="motivo-publicar")
        await user.should_see("o custodiante) não pode aprovar a versão")
        user.find(marker=f"autorizar-a1-{CERT}").click()
        await user.should_see("Robô autorizado neste portal.", retries=50)
        assert ("autorizar", "guarda@a") in credentials.calls
        user.find(marker="sair").click()
        await user.should_see(marker="pagina-login", retries=50)

        # Quem aprova vê "Aprovar" (falta mais uma pessoa); depois, "1 de 2 aprovações".
        await login(user, "aprovador", path)
        await user.should_see(marker="cartao-a1", retries=50)
        await user.should_not_see(marker=f"autorizar-a1-{CERT}")
        await user.should_see("Esta versão precisa da aprovação de mais uma pessoa depois da sua.")
        user.find(marker="publicar").click()
        await user.should_see("1 de 2 aprovações", retries=50)
        await user.should_see(marker="motivo-publicar")
        await user.should_see("Você já aprovou esta versão; falta a aprovação de outra pessoa.")
        user.find(marker="sair").click()
        await user.should_see(marker="pagina-login", retries=50)

        await login(user, "aprovadora", path)
        await user.should_see("1 de 2 aprovações", retries=50)
        user.find(marker="publicar").click()
        await user.should_see("Versão 1 publicada.", retries=50)
    published = await world.platform.get_version(world.principals["aprovador"], version.robot_id, 1)
    assert published is not None and published.approvals == ["aprovador@a", "aprovadora@a"]


async def test_no_approve_button_while_the_certificate_is_not_ready(world: World) -> None:
    """O servidor recusaria a aprovação (422 certificado_a1_bloqueado): a tela diz por quê
    em vez de mostrar o botão."""
    _people(world)
    credentials = MemoryCredentials(world)
    credentials.add(world.area_a, "guarda@a", 200)
    world.credentials = credentials
    version = await _a1_robot(world)
    async with world.user() as user:
        await login(user, "aprovador", f"/robos/{version.robot_id}")
        await user.should_see(marker="a1-pendencias", retries=50)
        await user.should_see(marker="motivo-publicar")
        await user.should_see("veja o que falta no cartão do certificado")
        await user.should_not_see(marker="publicar")
    assert ("autorizar", "guarda@a") not in credentials.calls


async def test_the_custodian_of_a_password_still_approves(world: World) -> None:
    """Só o custodiante de um certificado A1 do robô não aprova (como o servidor conta)."""
    _people(world)
    credentials = MemoryCredentials(world)
    credentials.add(world.area_a, "guarda@a", 200)
    credentials.add(world.area_a, "aprovadora@a", 200, name="portal", kind=CredentialKind.PASSWORD)
    world.credentials = credentials
    version = await _a1_robot(world)
    await credentials.authorize(world.principals["custodiante"], version.robot_id, CERT)
    async with world.user() as user:
        await login(user, "aprovadora", f"/robos/{version.robot_id}")
        await user.should_see(marker="cartao-a1", retries=50)
        await user.should_see(marker="publicar")
        await user.should_not_see(marker="motivo-publicar")


async def test_a_version_the_custodian_created_shows_no_approve_button(world: World) -> None:
    _people(world)
    credentials = MemoryCredentials(world)
    credentials.add(world.area_a, "criadora@a", 200)
    world.credentials = credentials
    version = await _a1_robot(world)
    # (O servidor não deixaria a criadora autorizar; aqui só a pendência some.)
    await credentials.authorize(world.principals["criadora"], version.robot_id, CERT)
    async with world.user() as user:
        await login(user, "aprovador", f"/robos/{version.robot_id}")
        await user.should_see(marker="motivo-publicar", retries=50)
        await user.should_see("foi criada pelo custodiante de um certificado A1")
        await user.should_not_see(marker="publicar")


async def test_a_company_admin_registers_in_an_area_the_screen_does_not_know(
    world: World,
) -> None:
    """Uma empresa sem robôs, sem credenciais e sem nomes de área configurados: quem
    administra a empresa inteira registra pelo identificador da área."""
    company_c, area_c = new_id(), new_id()
    world.principals["admin_c"] = Principal(
        subject="admin@c", company_id=company_c, company_roles=frozenset({Role.COMPANY_ADMIN})
    )
    credentials = MemoryCredentials(world)
    world.credentials = credentials
    async with world.user() as user:
        await login(user, "admin_c", "/credenciais")
        await user.should_see(marker="registrar-credencial", retries=50)
        # Nenhuma área de outra empresa aparece para escolher.
        await user.should_not_see("Fiscal")
        await user.should_not_see("Compras")
        user.find(marker="registrar").click()
        await user.should_see("Escolha uma área válida.", retries=50)
        assert credentials.calls == []
        for marker, value in (
            ("outra-area-credencial", str(area_c)),
            ("nome-credencial", "certificado_c"),
            ("origem-credencial", BASE),
            ("cofre-credencial", "cofre/c"),
            ("custodiante-credencial", "guarda@c"),
            ("validade-credencial", "31/12/2030"),
        ):
            user.find(marker=marker).type(value)
        user.find(marker="registrar").click()
        await user.should_see("Credencial registrada.", retries=50)
    assert credentials.calls == [("registrar", "admin@c")]
    assert credentials.registered_areas == [area_c]


async def test_no_repair_button_for_a1_robots(world: World) -> None:
    creator = world.principals["criadora"]
    info = await world.platform.create_version(creator, world.area_a, a1_manifest())
    await world.platform.mark_validated(creator, info.robot_id, 1)
    other = Principal(
        subject="segunda@a",
        company_id=world.company_a,
        area_roles={world.area_a: frozenset({Role.APPROVER})},
    )
    await world.platform.publish(world.principals["aprovador"], info.robot_id, 1)
    await world.platform.publish(other, info.robot_id, 1)
    status = await world.platform.start(
        world.principals["operador"], "protocolo_a1", INPUTS, RunTrigger.MANUAL
    )
    world.platform.set_run_state(
        status.run_id, RunState.FAILED, error_code="elemento_nao_encontrado", error_message="x"
    )
    async with world.user() as user:
        await login(user, "criadora", f"/execucoes/{status.run_id}")
        await user.should_see(marker="erro-execucao", retries=50)
        await user.should_not_see(marker="consertar")


def test_permissions_mirror_the_server() -> None:
    company, area = new_id(), new_id()
    approver = Principal(
        subject="ap", company_id=company, area_roles={area: frozenset({Role.APPROVER})}
    )
    custodian = Principal(
        subject="guarda",
        company_id=company,
        area_roles={area: frozenset({Role.APPROVER, Role.CREDENTIAL_CUSTODIAN})},
    )
    now = datetime.now(UTC)
    version = RobotVersionInfo(
        robot_id=new_id(),
        version_id=new_id(),
        version_no=1,
        status="validated",  # type: ignore[arg-type]
        manifest=a1_manifest(),
        fingerprint="0" * 64,
        created_by="cria",
        approved_by=None,
        created_at=now,
        approvals=["ap"],
        approvals_required=2,
    )
    assert publish_block_reason(approver, area, version) == (
        "Você já aprovou esta versão; falta a aprovação de outra pessoa."
    )
    reason = publish_block_reason(custodian, area, version, {"guarda"})
    assert reason is not None and "custodiante" in reason
    assert publish_block_reason(custodian, area, version, set()) is None
    # O que falta no certificado também barra (o servidor recusaria com 422).
    pending = ["falta a autorização do custodiante do certificado 'certificado' para este robô"]
    reason = publish_block_reason(custodian, area, version, set(), pending)
    assert reason is not None and "ainda não pode ser usado" in reason
    # A versão criada por um custodiante do certificado não é aprovada.
    reason = publish_block_reason(custodian, area, version, {"cria"})
    assert reason is not None and "criada pelo custodiante" in reason
    # Num robô sem A1, nada disso conta.
    plain = version.model_copy(
        update={"manifest": emitir_protocolo(BASE, HOST), "approvals": [], "approvals_required": 1}
    )
    assert publish_block_reason(custodian, area, plain, {"guarda", "cria"}, pending) is None
    credential = CredentialInfo(
        credential_id=new_id(),
        area_id=area,
        name=CERT,
        kind=CredentialKind.PFX_A1,
        origin=BASE,
        custodian_subject="guarda",
        status=CredentialStatus.ACTIVE,
        created_at=now,
        updated_at=now,
    )
    assert is_custodian(custodian, credential) and not is_custodian(approver, credential)
    assert can_revoke_credential(custodian, credential)
    assert not can_revoke_credential(approver, credential)
    # Os custodiantes que não aprovam são só os dos certificados A1 (como no servidor).
    password = credential.model_copy(
        update={"name": "portal", "kind": CredentialKind.PASSWORD, "custodian_subject": "ap"}
    )
    context = A1Context(credentials={CERT: credential, "portal": password})
    assert context.custodians == {"guarda"}
    # O conserto: nunca para A1, e sem o manifesto o botão não aparece.
    failed = RunStatus(
        run_id=new_id(),
        robot_slug="x",
        version_no=1,
        state=RunState.FAILED,
        trigger=RunTrigger.MANUAL,
        created_at=now,
        error_code="elemento_nao_encontrado",
    )
    assert can_repair(failed, emitir_protocolo(BASE, HOST))
    assert not can_repair(failed, a1_manifest())
    assert not can_repair(failed, None)


def test_parse_validity() -> None:
    assert parse_validity("31/12/2030") is not None
    end = parse_validity("2030-12-31")
    assert end is not None and end.hour == 23 and end.tzinfo is not None
    assert parse_validity("31/02/2030") is None
    assert parse_validity("amanhã") is None
    assert parse_validity("") is None
