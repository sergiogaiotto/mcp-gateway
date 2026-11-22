"""Credenciais registradas e as regras do certificado A1 (``PgCredentials`` e a publicação)."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from rapa.control.db import Database
from rapa.control.services import PgCredentials, PgProvisioning
from rapa.control.services.robots import ALREADY_APPROVED, CUSTODIAN_APPROVER, CUSTODIAN_CREATOR
from rapa.core.contracts import (
    CertificateBlocked,
    Conflict,
    CredentialStatus,
    Forbidden,
    InvalidInput,
    NotFound,
    Role,
    VersionStatus,
)
from rapa.core.robot import CredentialKind, CredentialRef
from tests.control.db.support import protocolo, publish

from .support import (
    CERT,
    FINGERPRINT,
    ORIGIN,
    OTHER_FINGERPRINT,
    A1World,
    a1_protocolo,
    a1_world,
    draft_a1,
    in_days,
    person,
    publish_a1,
    ready_a1,
    register_a1,
    set_not_after,
)

pytestmark = pytest.mark.database


async def _audit_actions(superuser: AsyncEngine, company_id: object) -> list[str]:
    async with superuser.connect() as conn:
        rows = await conn.execute(
            text("SELECT action FROM audit_log WHERE company_id = :c ORDER BY seq"),
            {"c": company_id},
        )
        return [row[0] for row in rows]


# -- registro -------------------------------------------------------------------------


async def test_register_only_by_the_area_admin_with_a_custodian(
    app_db: Database, superuser_engine: AsyncEngine
) -> None:
    world = await a1_world(app_db, "Registro")
    tenant = world.tenant
    credentials = world.credentials
    base = {
        "name": CERT,
        "kind": CredentialKind.PFX_A1,
        "origin": "HTTP://Portal-V1.rapa.test:8080/",
        "vault_path": "empresas/x/a1",
        "custodian_subject": world.custodian.subject,
        "not_after": in_days(200),
        "fingerprint": ":".join(["AB"] * 32),
    }
    # Quem cria robôs não registra; outra área e outra empresa não existem.
    with pytest.raises(Forbidden):
        await credentials.register(tenant.creator, tenant.area_id, **base)
    with pytest.raises(NotFound):
        await credentials.register(tenant.outsider, tenant.area_id, **base)
    other = await a1_world(app_db, "Registro de outra empresa")
    with pytest.raises(NotFound):
        await credentials.register(other.area_admin, tenant.area_id, **base)

    # O A1 precisa do custodiante e da validade no futuro.
    for changes, detail in (
        ({"custodian_subject": None}, "custodiante: obrigatório para o certificado A1"),
        ({"not_after": None}, "validade: obrigatória para o certificado A1"),
        ({"not_after": in_days(-1)}, "validade: a data já passou"),
        ({"origin": "https://portal.rapa.test/caminho"}, "origem: a origem da credencial"),
        ({"vault_path": "empresas/../outra"}, "caminho no cofre"),
        ({"fingerprint": "123"}, "impressão: use a impressão SHA-256"),
        ({"name": "Certificado"}, "nome: use letras minúsculas"),
    ):
        with pytest.raises(InvalidInput) as caught:
            await credentials.register(world.area_admin, tenant.area_id, **{**base, **changes})
        assert any(e.startswith(detail) for e in caught.value.errors), (
            changes,
            caught.value.errors,
        )

    with pytest.raises(InvalidInput, match="credencial inválida"):
        await credentials.register(world.area_admin, tenant.area_id, **{**base, "kind": "chave"})

    # O custodiante é um usuário ativo, com o papel na área (ou na empresa), e não quem registra.
    for subject in (
        tenant.creator.subject,  # sem o papel de custodiante
        world.other_custodian.subject,  # custodiante só da outra área
        "ninguem@a1",  # não existe
    ):
        with pytest.raises(InvalidInput) as caught:
            await credentials.register(
                world.area_admin, tenant.area_id, **{**base, "custodian_subject": subject}
            )
        assert caught.value.errors[0].startswith("custodiante:")
    both = await person(
        app_db,
        tenant,
        "admin-e-guarda",
        (Role.AREA_ADMIN, tenant.area_id),
        (Role.CREDENTIAL_CUSTODIAN, tenant.area_id),
    )
    with pytest.raises(Forbidden, match="não pode ser o custodiante"):
        await credentials.register(
            both, tenant.area_id, **{**base, "custodian_subject": both.subject}
        )
    inactive = await person(app_db, tenant, "inativa", (Role.CREDENTIAL_CUSTODIAN, tenant.area_id))
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET active = false WHERE subject = :s"), {"s": inactive.subject}
        )
    with pytest.raises(InvalidInput):
        await credentials.register(
            world.area_admin, tenant.area_id, **{**base, "custodian_subject": inactive.subject}
        )

    info = await credentials.register(world.area_admin, tenant.area_id, **base)
    # A origem passa pela normalização do manifesto; a impressão vira hexadecimal minúsculo.
    assert info.origin == ORIGIN
    assert info.fingerprint_sha256 == FINGERPRINT
    assert info.status is CredentialStatus.ACTIVE
    assert info.custodian_subject == world.custodian.subject
    assert info.vault_path == "empresas/x/a1"
    with pytest.raises(Conflict):
        await credentials.register(world.area_admin, tenant.area_id, **base)

    # Um custodiante da empresa inteira serve; o administrador da empresa também registra.
    company_wide = await person(app_db, tenant, "guarda-geral", (Role.CREDENTIAL_CUSTODIAN, None))
    other_cert = await credentials.register(
        tenant.admin,
        tenant.area_id,
        **{**base, "name": "certificado_2", "custodian_subject": company_wide.subject},
    )
    assert other_cert.custodian_subject == company_wide.subject
    # Senha não precisa de custodiante nem de validade.
    password = await credentials.register(
        world.area_admin,
        tenant.area_id,
        name="portal",
        kind=CredentialKind.PASSWORD,
        origin=ORIGIN,
        vault_path="empresas/x/portal",
    )
    assert password.status is CredentialStatus.ACTIVE and password.not_after is None
    assert (await _audit_actions(superuser_engine, tenant.company_id)).count(
        "credencial.registrada"
    ) == 3


async def test_list_hides_the_vault_path_and_other_areas(app_db: Database) -> None:
    world = await a1_world(app_db, "Lista")
    tenant = world.tenant
    await register_a1(world)
    for principal, sees_path in (
        (world.area_admin, True),
        (world.custodian, True),
        (tenant.admin, True),
        (tenant.operator, False),
        (tenant.viewer, False),
        (tenant.approver, False),
    ):
        listed = await world.credentials.list(principal)
        assert [c.name for c in listed] == [CERT], principal.subject
        assert (listed[0].vault_path is not None) is sees_path, principal.subject
        assert listed[0].not_after is not None and listed[0].custodian_subject
    # Quem só tem papel na outra área não vê; pedir a área direto é 404.
    assert await world.credentials.list(tenant.outsider) == []
    with pytest.raises(NotFound):
        await world.credentials.list(tenant.outsider, tenant.area_id)
    assert len(await world.credentials.list(tenant.operator, tenant.area_id)) == 1


async def test_expiring_lists_soon_expired_and_legacy_rows(
    app_db: Database, superuser_engine: AsyncEngine
) -> None:
    world = await a1_world(app_db, "Vencimentos")
    tenant = world.tenant
    soon = await register_a1(world, not_after=in_days(10))
    later = await world.credentials.register(
        world.area_admin,
        tenant.area_id,
        name="certificado_longo",
        kind=CredentialKind.PFX_A1,
        origin=ORIGIN,
        vault_path="empresas/x/longo",
        custodian_subject=world.custodian.subject,
        not_after=in_days(300),
    )
    gone = await world.credentials.register(
        world.area_admin,
        tenant.area_id,
        name="certificado_velho",
        kind=CredentialKind.PFX_A1,
        origin=ORIGIN,
        vault_path="empresas/x/velho",
        custodian_subject=world.custodian.subject,
        not_after=in_days(5),
    )
    await set_not_after(superuser_engine, gone.credential_id, in_days(-3))
    # O cadastro de plataforma também registra (com a validade, que o banco exige no A1).
    await PgProvisioning(app_db).register_credential(
        tenant.company_id,
        tenant.area_id,
        CredentialRef(name="certificado_cadastro", kind="pfx_a1", origin=ORIGIN),
        vault_path="empresas/x/cadastro",
        not_after=in_days(20),
    )
    # Sem a validade, um A1 novo é recusado pelo banco (as linhas antigas ficam: ver a migração).
    with pytest.raises(IntegrityError):
        await PgProvisioning(app_db).register_credential(
            tenant.company_id,
            tenant.area_id,
            CredentialRef(name="certificado_sem_data", kind="pfx_a1", origin=ORIGIN),
            vault_path="empresas/x/sem-data",
        )

    expiring = await world.credentials.expiring(world.custodian)
    assert [c.name for c in expiring] == ["certificado_velho", CERT, "certificado_cadastro"]
    statuses = {c.name: c.status for c in expiring}
    assert statuses == {
        "certificado_velho": CredentialStatus.EXPIRED,
        CERT: CredentialStatus.EXPIRING,
        "certificado_cadastro": CredentialStatus.EXPIRING,
    }
    assert later.status is CredentialStatus.ACTIVE
    assert soon.status is CredentialStatus.EXPIRING
    assert len(await world.credentials.expiring(world.custodian, 400)) == 4
    assert await world.credentials.expiring(tenant.outsider) == []
    with pytest.raises(InvalidInput):
        await world.credentials.expiring(world.custodian, -1)
    # Um certificado revogado não entra.
    await world.credentials.revoke(world.area_admin, soon.credential_id)
    assert CERT not in {c.name for c in await world.credentials.expiring(world.custodian)}


# -- renovação e revogação -----------------------------------------------------------


async def test_renew_only_by_the_custodian_and_a_new_fingerprint_drops_authorizations(
    app_db: Database, superuser_engine: AsyncEngine
) -> None:
    world = await a1_world(app_db, "Renovação")
    version = await ready_a1(world)
    credential = (await world.credentials.list(world.custodian))[0]
    new_date = in_days(700)
    for principal in (world.area_admin, world.tenant.admin, world.tenant.approver):
        with pytest.raises(Forbidden):
            await world.credentials.renew(
                principal, credential.credential_id, not_after=new_date, fingerprint=FINGERPRINT
            )
    with pytest.raises(InvalidInput):
        await world.credentials.renew(
            world.custodian, credential.credential_id, not_after=in_days(-1)
        )

    # A mesma impressão: a autorização continua valendo.
    same = await world.credentials.renew(
        world.custodian, credential.credential_id, not_after=new_date, fingerprint=FINGERPRINT
    )
    assert same.not_after == new_date
    [authorization] = await world.credentials.list_authorizations(world.custodian, version.robot_id)
    assert authorization.current and authorization.revoked_at is None

    # Outra impressão (o certificado foi trocado): a autorização cai.
    await world.credentials.renew(
        world.custodian,
        credential.credential_id,
        not_after=new_date,
        fingerprint=OTHER_FINGERPRINT,
    )
    [dropped] = await world.credentials.list_authorizations(world.custodian, version.robot_id)
    assert dropped.revoked_at is not None and not dropped.current
    assert dropped.revoked_by == world.custodian.subject
    with pytest.raises(CertificateBlocked) as caught:
        await world.platform.publish(world.tenant.approver, version.robot_id, 1)
    assert any("falta a autorização" in e for e in caught.value.errors)

    # De novo autorizado com a impressão nova. Renovar sem a impressão, com uma registrada,
    # é recusado: não apaga a impressão nem adivinha se o certificado mudou.
    await world.credentials.authorize(world.custodian, version.robot_id, CERT)
    with pytest.raises(InvalidInput) as missing:
        await world.credentials.renew(world.custodian, credential.credential_id, not_after=new_date)
    assert any("impressão" in e for e in missing.value.errors)
    [kept] = await world.credentials.list(world.custodian)
    assert kept.fingerprint_sha256 == OTHER_FINGERPRINT
    authorizations = await world.credentials.list_authorizations(world.custodian, version.robot_id)
    assert [a.revoked_at is not None for a in authorizations] == [False, True]
    actions = await _audit_actions(superuser_engine, world.tenant.company_id)
    assert actions.count("credencial.renovada") == 2
    assert actions.count("credencial.a1_autorizacao_revogada") == 1

    # Senha não tem validade para renovar.
    password = await world.credentials.register(
        world.area_admin,
        world.tenant.area_id,
        name="portal",
        kind=CredentialKind.PASSWORD,
        origin=ORIGIN,
        vault_path="empresas/x/portal",
        custodian_subject=world.custodian.subject,
    )
    with pytest.raises(InvalidInput):
        await world.credentials.renew(world.custodian, password.credential_id, not_after=new_date)


async def test_renew_without_fingerprint_keeps_a_certificate_registered_without_one(
    app_db: Database, superuser_engine: AsyncEngine
) -> None:
    """Sem impressão registrada e sem impressão na renovação, a impressão não mudou: as
    autorizações continuam. A primeira impressão informada é uma troca e as derruba."""
    world = await a1_world(app_db, "Renovação sem impressão")
    await register_a1(world, fingerprint=None)
    version = await draft_a1(world)
    await world.credentials.authorize(world.custodian, version.robot_id, CERT)
    [credential] = await world.credentials.list(world.custodian)
    renewed = await world.credentials.renew(
        world.custodian, credential.credential_id, not_after=in_days(500)
    )
    assert renewed.fingerprint_sha256 is None and renewed.not_after is not None
    [authorization] = await world.credentials.list_authorizations(world.custodian, version.robot_id)
    assert authorization.current and authorization.revoked_at is None
    # A aprovação conta: o certificado continua pronto para uso.
    first = await world.platform.publish(world.tenant.approver, version.robot_id, 1)
    assert first.approvals == [world.tenant.approver.subject]

    await world.credentials.renew(
        world.custodian, credential.credential_id, not_after=in_days(600), fingerprint=FINGERPRINT
    )
    [dropped] = await world.credentials.list_authorizations(world.custodian, version.robot_id)
    assert dropped.revoked_at is not None and not dropped.current
    actions = await _audit_actions(superuser_engine, world.tenant.company_id)
    assert actions.count("credencial.renovada") == 2
    assert actions.count("credencial.a1_autorizacao_revogada") == 1


async def test_revoke_by_custodian_or_area_admin_revokes_authorizations(
    app_db: Database,
) -> None:
    world = await a1_world(app_db, "Revogação")
    version = await ready_a1(world)
    credential = (await world.credentials.list(world.custodian))[0]
    for principal in (world.tenant.approver, world.tenant.operator, world.tenant.creator):
        with pytest.raises(Forbidden):
            await world.credentials.revoke(principal, credential.credential_id)
    with pytest.raises(NotFound):
        await world.credentials.revoke(world.tenant.outsider, credential.credential_id)
    revoked = await world.credentials.revoke(world.area_admin, credential.credential_id)
    assert revoked.status is CredentialStatus.REVOKED
    assert revoked.revoked_by == world.area_admin.subject
    [authorization] = await world.credentials.list_authorizations(world.custodian, version.robot_id)
    assert authorization.revoked_at is not None and not authorization.current
    with pytest.raises(Conflict):
        await world.credentials.revoke(world.custodian, credential.credential_id)
    with pytest.raises(Conflict):
        await world.credentials.renew(
            world.custodian, credential.credential_id, not_after=in_days(10)
        )
    with pytest.raises(Conflict):
        await world.credentials.authorize(world.custodian, version.robot_id, CERT)


# -- autorização do custodiante ------------------------------------------------------


async def test_authorize_rules(app_db: Database, superuser_engine: AsyncEngine) -> None:
    world = await a1_world(app_db, "Autorizar")
    tenant = world.tenant
    credential = await register_a1(world)
    version = await draft_a1(world)
    robot_id = version.robot_id
    # Só o custodiante, e o robô é da área do certificado.
    for principal in (world.area_admin, tenant.admin, tenant.approver, world.other_custodian):
        with pytest.raises((Forbidden, NotFound)):
            await world.credentials.authorize(principal, robot_id, CERT)
    with pytest.raises(NotFound):
        await world.credentials.authorize(world.custodian, robot_id, "outro_certificado")
    other = await a1_world(app_db, "Autorizar em outra empresa")
    with pytest.raises(NotFound):
        await world.credentials.authorize(other.custodian, robot_id, CERT)

    # A versão mais nova precisa declarar o certificado com a mesma origem registrada.
    moved = await world.platform.create_version(
        tenant.creator, tenant.area_id, a1_protocolo(origin="https://outro-portal.rapa.test")
    )
    with pytest.raises(InvalidInput, match="outra origem"):
        await world.credentials.authorize(world.custodian, robot_id, CERT)
    assert moved.version_no == 2
    without = await world.platform.create_version(tenant.creator, tenant.area_id, protocolo())
    assert without.version_no == 3
    with pytest.raises(InvalidInput, match="não declara"):
        await world.credentials.authorize(world.custodian, robot_id, CERT)
    await world.platform.create_version(tenant.creator, tenant.area_id, a1_protocolo())

    # Vencido não se autoriza.
    await set_not_after(superuser_engine, credential.credential_id, in_days(-1))
    with pytest.raises(Conflict, match="venceu"):
        await world.credentials.authorize(world.custodian, robot_id, CERT)
    await set_not_after(superuser_engine, credential.credential_id, in_days(100))

    authorization = await world.credentials.authorize(world.custodian, robot_id, CERT)
    assert authorization.current and authorization.fingerprint_sha256 == FINGERPRINT
    assert authorization.origin == ORIGIN and authorization.authorized_by == world.custodian.subject
    with pytest.raises(Conflict):
        await world.credentials.authorize(world.custodian, robot_id, CERT)
    # Qualquer papel na área vê as autorizações; outra área não.
    assert len(await world.credentials.list_authorizations(tenant.viewer, robot_id)) == 1
    with pytest.raises(NotFound):
        await world.credentials.list_authorizations(tenant.outsider, robot_id)

    # Só o custodiante ou quem administra a área revoga a autorização.
    with pytest.raises(Forbidden):
        await world.credentials.revoke_authorization(
            tenant.approver, authorization.authorization_id
        )
    with pytest.raises(NotFound):
        await world.credentials.revoke_authorization(
            world.custodian, authorization.authorization_id, robot_id=UUID(int=1)
        )
    revoked = await world.credentials.revoke_authorization(
        world.area_admin, authorization.authorization_id, robot_id=robot_id
    )
    assert revoked.revoked_by == world.area_admin.subject and not revoked.current
    with pytest.raises(Conflict):
        await world.credentials.revoke_authorization(
            world.custodian, authorization.authorization_id
        )
    # Senha não precisa de autorização por robô.
    await world.credentials.register(
        world.area_admin,
        tenant.area_id,
        name="portal",
        kind=CredentialKind.PASSWORD,
        origin=ORIGIN,
        vault_path="empresas/x/portal",
        custodian_subject=world.custodian.subject,
    )
    with pytest.raises(InvalidInput):
        await world.credentials.authorize(world.custodian, robot_id, "portal")


async def test_the_custodian_never_authorizes_a_version_they_created(app_db: Database) -> None:
    world = await a1_world(app_db, "Custodiante que cria")
    tenant = world.tenant
    creator_custodian = await person(
        app_db,
        tenant,
        "cria-e-guarda",
        (Role.CREATOR, tenant.area_id),
        (Role.CREDENTIAL_CUSTODIAN, tenant.area_id),
    )
    await world.credentials.register(
        world.area_admin,
        tenant.area_id,
        name=CERT,
        kind=CredentialKind.PFX_A1,
        origin=ORIGIN,
        vault_path="empresas/x/a1",
        custodian_subject=creator_custodian.subject,
        not_after=in_days(100),
    )
    version = await world.platform.create_version(creator_custodian, tenant.area_id, a1_protocolo())
    with pytest.raises(Forbidden, match="ele mesmo criou"):
        await world.credentials.authorize(creator_custodian, version.robot_id, CERT)
    # Uma versão nova de outra pessoa: aí ele autoriza.
    await world.platform.create_version(tenant.creator, tenant.area_id, a1_protocolo())
    authorization = await world.credentials.authorize(creator_custodian, version.robot_id, CERT)
    assert authorization.current
    # Mas a versão antiga, que ele criou, não é aprovada: rodaria com a autorização dele.
    await world.platform.mark_validated(creator_custodian, version.robot_id, 1)
    with pytest.raises(Forbidden) as caught:
        await world.platform.publish(tenant.approver, version.robot_id, 1)
    assert caught.value.message == CUSTODIAN_CREATOR
    info = await world.platform.get_version(tenant.viewer, version.robot_id, 1)
    assert info is not None and info.approvals == [] and info.status is VersionStatus.VALIDATED


async def test_a_version_the_custodian_creates_after_authorizing_is_never_approved(
    app_db: Database,
) -> None:
    """A autorização é do robô: depois de autorizar, o custodiante que também cria robôs
    não consegue pôr no ar uma versão dele (nem com duas outras pessoas aprovando)."""
    world = await a1_world(app_db, "Custodiante que cria depois")
    tenant = world.tenant
    creator_custodian = await person(
        app_db,
        tenant,
        "cria-e-guarda",
        (Role.CREATOR, tenant.area_id),
        (Role.CREDENTIAL_CUSTODIAN, tenant.area_id),
    )
    await world.credentials.register(
        world.area_admin,
        tenant.area_id,
        name=CERT,
        kind=CredentialKind.PFX_A1,
        origin=ORIGIN,
        vault_path="empresas/x/a1",
        custodian_subject=creator_custodian.subject,
        not_after=in_days(100),
        fingerprint=FINGERPRINT,
    )
    first = await draft_a1(world)
    robot_id = first.robot_id
    await world.credentials.authorize(creator_custodian, robot_id, CERT)
    await world.platform.publish(tenant.approver, robot_id, 1)
    published = await world.platform.publish(world.approver2, robot_id, 1)
    assert published.status is VersionStatus.PUBLISHED

    mine = await world.platform.create_version(creator_custodian, tenant.area_id, a1_protocolo())
    await world.platform.mark_validated(creator_custodian, robot_id, mine.version_no)
    for approver in (tenant.approver, world.approver2):
        with pytest.raises(Forbidden) as caught:
            await world.platform.publish(approver, robot_id, mine.version_no)
        assert caught.value.message == CUSTODIAN_CREATOR
    info = await world.platform.get_version(tenant.viewer, robot_id, mine.version_no)
    assert info is not None and info.approvals == [] and info.status is VersionStatus.VALIDATED
    current = await world.platform.get_published(tenant.operator, "emitir_protocolo")
    assert current is not None and current.version_no == 1


async def test_the_custodian_never_authorizes_a_robot_whose_published_version_they_created(
    app_db: Database, superuser_engine: AsyncEngine
) -> None:
    """A versão que roda é a publicada, que pode não ser a mais nova (uma publicada antes
    das regras do A1, por exemplo)."""
    world = await a1_world(app_db, "Custodiante da versão publicada")
    tenant = world.tenant
    creator_custodian = await person(
        app_db,
        tenant,
        "cria-e-guarda",
        (Role.CREATOR, tenant.area_id),
        (Role.CREDENTIAL_CUSTODIAN, tenant.area_id),
    )
    old = await world.platform.create_version(creator_custodian, tenant.area_id, a1_protocolo())
    # Publicada antes das regras novas (sem as aprovações de hoje): direto no banco.
    async with superuser_engine.begin() as conn:
        await conn.execute(text("SET LOCAL session_replication_role = replica"))
        await conn.execute(
            text("UPDATE robot_versions SET status = 'published', approved_by = :a WHERE id = :v"),
            {"a": tenant.approver.subject, "v": old.version_id},
        )
        await conn.execute(
            text("UPDATE robots SET status = 'published', current_version_no = 1 WHERE id = :r"),
            {"r": old.robot_id},
        )
    await world.credentials.register(
        world.area_admin,
        tenant.area_id,
        name=CERT,
        kind=CredentialKind.PFX_A1,
        origin=ORIGIN,
        vault_path="empresas/x/a1",
        custodian_subject=creator_custodian.subject,
        not_after=in_days(100),
    )
    # A mais nova é de outra pessoa, mas a publicada é dele.
    await world.platform.create_version(tenant.creator, tenant.area_id, a1_protocolo())
    with pytest.raises(Forbidden, match="ele mesmo criou"):
        await world.credentials.authorize(creator_custodian, old.robot_id, CERT)
    assert await world.credentials.list_authorizations(tenant.viewer, old.robot_id) == []


# -- duas aprovações -------------------------------------------------------------------


async def test_a1_version_needs_two_different_approvers(
    app_db: Database, superuser_engine: AsyncEngine
) -> None:
    world = await a1_world(app_db, "Dois aprovadores")
    tenant = world.tenant
    version = await ready_a1(world)
    robot_id, number = version.robot_id, version.version_no
    assert version.approvals_required == 2 and version.approvals == []

    first = await world.platform.publish(tenant.approver, robot_id, number)
    assert first.status is VersionStatus.VALIDATED
    assert first.approvals == [tenant.approver.subject] and first.approved_by is None
    # A mesma pessoa de novo: conflito, com a mensagem que diz o que falta.
    with pytest.raises(Conflict) as caught:
        await world.platform.publish(tenant.approver, robot_id, number)
    assert caught.value.message == ALREADY_APPROVED
    pending = await world.credentials.approvals(tenant.viewer, robot_id, number)
    assert (pending.required, pending.missing, pending.problems) == (2, 1, [])
    assert [a.approved_by for a in pending.approvals] == [tenant.approver.subject]
    listed = await world.platform.get_version(tenant.viewer, robot_id, number)
    assert listed is not None and listed.approvals == [tenant.approver.subject]

    second = await world.platform.publish(world.approver2, robot_id, number)
    assert second.status is VersionStatus.PUBLISHED
    assert second.approved_by == world.approver2.subject
    assert second.approvals == [tenant.approver.subject, world.approver2.subject]
    done = await world.credentials.approvals(tenant.viewer, robot_id, number)
    assert done.missing == 0 and done.status is VersionStatus.PUBLISHED
    actions = await _audit_actions(superuser_engine, tenant.company_id)
    assert actions.count("robo.versao_aprovada") == 1
    assert actions.count("robo.versao_publicada") == 1


async def test_the_custodian_cannot_approve_and_the_creator_still_cannot(app_db: Database) -> None:
    world = await a1_world(app_db, "Separação de funções")
    tenant = world.tenant
    approver_custodian = await person(
        app_db,
        tenant,
        "aprova-e-guarda",
        (Role.APPROVER, tenant.area_id),
        (Role.CREDENTIAL_CUSTODIAN, tenant.area_id),
    )
    await world.credentials.register(
        world.area_admin,
        tenant.area_id,
        name=CERT,
        kind=CredentialKind.PFX_A1,
        origin=ORIGIN,
        vault_path="empresas/x/a1",
        custodian_subject=approver_custodian.subject,
        not_after=in_days(100),
        fingerprint=FINGERPRINT,
    )
    version = await draft_a1(world)
    await world.credentials.authorize(approver_custodian, version.robot_id, CERT)
    with pytest.raises(Forbidden) as caught:
        await world.platform.publish(approver_custodian, version.robot_id, 1)
    assert caught.value.message == CUSTODIAN_APPROVER
    with pytest.raises(Forbidden):
        await world.platform.publish(tenant.creator, version.robot_id, 1)
    # Quem só opera também não aprova.
    with pytest.raises(Forbidden):
        await world.platform.publish(tenant.operator, version.robot_id, 1)
    info = await world.platform.get_version(tenant.viewer, version.robot_id, 1)
    assert info is not None and info.approvals == [] and info.status is VersionStatus.VALIDATED


@pytest.mark.parametrize("problem", ["sem_registro", "revogado", "vencido", "sem_autorizacao"])
async def test_a1_problems_block_the_approval(
    app_db: Database, superuser_engine: AsyncEngine, problem: str
) -> None:
    world = await a1_world(app_db, f"Bloqueio {problem}")
    tenant = world.tenant
    credential = None if problem == "sem_registro" else await register_a1(world)
    version = await draft_a1(world)
    if credential is not None and problem != "sem_autorizacao":
        await world.credentials.authorize(world.custodian, version.robot_id, CERT)
    if credential is not None and problem == "revogado":
        await world.credentials.revoke(world.custodian, credential.credential_id)
    if credential is not None and problem == "vencido":
        await set_not_after(superuser_engine, credential.credential_id, in_days(-1))
    with pytest.raises(CertificateBlocked) as caught:
        await world.platform.publish(tenant.approver, version.robot_id, 1)
    expected = {
        "sem_registro": "não está registrado na área do robô",
        "revogado": "foi revogado",
        "vencido": "venceu em",
        "sem_autorizacao": "falta a autorização do custodiante",
    }[problem]
    assert any(expected in e for e in caught.value.errors), caught.value.errors
    # Nenhum caminho no cofre, nenhum segredo na resposta.
    assert "empresas/" not in " ".join(caught.value.errors)
    info = await world.platform.get_version(tenant.viewer, version.robot_id, 1)
    assert info is not None and info.approvals == []
    approvals = await world.credentials.approvals(tenant.viewer, version.robot_id, 1)
    assert approvals.problems == caught.value.errors


async def test_a1_registered_for_another_origin_blocks(app_db: Database) -> None:
    world = await a1_world(app_db, "Outra origem")
    await register_a1(world, origin="https://outro-portal.rapa.test")
    version = await draft_a1(world)
    with pytest.raises(CertificateBlocked) as caught:
        await world.platform.publish(world.tenant.approver, version.robot_id, 1)
    assert any("registrado para https://outro-portal.rapa.test" in e for e in caught.value.errors)


async def test_version_without_a1_still_needs_one_approver(
    app_db: Database, superuser_engine: AsyncEngine
) -> None:
    world = await a1_world(app_db, "Um aprovador")
    published = await publish(world.platform, world.tenant, protocolo())
    assert published.status is VersionStatus.PUBLISHED
    assert published.approvals == [world.tenant.approver.subject]
    assert published.approvals_required == 1
    async with superuser_engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT approved_by FROM version_approvals WHERE robot_id = :r"),
                {"r": published.robot_id},
            )
        ).all()
    assert [r[0] for r in rows] == [world.tenant.approver.subject]


async def test_publish_a1_end_to_end(app_db: Database) -> None:
    world = await a1_world(app_db, "Publicar A1")
    published = await publish_a1(world)
    assert published.status is VersionStatus.PUBLISHED
    summary = await world.platform.get_published(world.tenant.operator, "emitir_protocolo")
    assert summary is not None and summary.version_no == 1


def test_credentials_service_checks_the_warning_window() -> None:
    with pytest.raises(ValueError):
        PgCredentials(None, warn_days=0)  # type: ignore[arg-type]


async def test_status_follows_the_clock(app_db: Database) -> None:
    world = await a1_world(app_db, "Relógio")
    await register_a1(world, not_after=in_days(40))
    later = PgCredentials(app_db, clock=lambda: datetime.now(UTC) + timedelta(days=15))
    [info] = await later.list(world.custodian)
    assert info.status is CredentialStatus.EXPIRING
    gone = PgCredentials(app_db, clock=lambda: datetime.now(UTC) + timedelta(days=41))
    [info] = await gone.list(world.custodian)
    assert info.status is CredentialStatus.EXPIRED
    assert isinstance(world, A1World)
