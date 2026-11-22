"""Ciclo de vida completo pelos serviços do PostgreSQL, com as regras da plataforma em memória."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from rapa.control.db import Database
from rapa.control.services import (
    PgPlatform,
    PgPrincipalResolver,
    PgProvisioning,
    verify_chain,
)
from rapa.core.contracts import (
    Conflict,
    Forbidden,
    InvalidInput,
    NotFound,
    QuotaExceeded,
    RobotStatus,
    Role,
    RunState,
    RunTrigger,
    VersionStatus,
)
from rapa.core.ids import new_id
from rapa.core.robot import CredentialRef, Effect
from rapa.fixtures.robots import emitir_protocolo

from .support import (
    BASE,
    PROTOCOLO_INPUTS,
    TENANT_AUDIT_ROWS,
    make_tenant,
    notas,
    protocolo,
    publish,
    without_provisioning,
)

pytestmark = pytest.mark.database


async def test_full_lifecycle(app_db: Database) -> None:
    platform = PgPlatform(app_db)
    tenant = await make_tenant(app_db, "Ciclo")

    draft = await platform.create_version(tenant.creator, tenant.area_id, protocolo())
    assert draft.version_no == 1
    assert draft.status is VersionStatus.DRAFT
    assert draft.violations == []
    assert draft.created_by == tenant.creator.subject
    assert draft.fingerprint == protocolo().fingerprint()
    assert draft.manifest == protocolo()

    robots = await platform.list_robots(tenant.creator)
    assert [(r.slug, r.status, r.current_version_no) for r in robots] == [
        ("emitir_protocolo", RobotStatus.DRAFT, None)
    ]
    assert await platform.list_published(tenant.operator) == []

    validated = await platform.mark_validated(tenant.creator, draft.robot_id, 1)
    assert validated.status is VersionStatus.VALIDATED
    published = await platform.publish(tenant.approver, draft.robot_id, 1)
    assert published.status is VersionStatus.PUBLISHED
    assert published.approved_by == tenant.approver.subject

    catalog = await platform.list_published(tenant.operator)
    assert [s.slug for s in catalog] == ["emitir_protocolo"]
    assert catalog[0].effect is Effect.IRREVERSIBLE
    assert catalog[0].input_schema == protocolo().input_json_schema()
    assert catalog[0].company_id == tenant.company_id
    found = await platform.get_published(tenant.operator, "emitir_protocolo")
    assert found is not None and found.version_no == 1

    run = await platform.start(
        tenant.operator, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.REST
    )
    assert run.state is RunState.QUEUED
    assert run.trigger is RunTrigger.REST
    assert run.version_no == 1
    assert run.queue_position == 1
    second = await platform.start(
        tenant.operator, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.BATCH
    )
    assert second.queue_position == 2  # lote vem depois da API
    again = await platform.get(tenant.operator, run.run_id)
    assert again == run

    # Nova versão: a anterior fica substituída e o catálogo mostra a nova.
    v2 = await platform.create_version(tenant.creator, tenant.area_id, protocolo())
    assert v2.version_no == 2 and v2.robot_id == draft.robot_id
    await platform.mark_validated(tenant.approver, draft.robot_id, 2)
    await platform.publish(tenant.approver, draft.robot_id, 2)
    old = await platform.get_version(tenant.creator, draft.robot_id, 1)
    assert old is not None and old.status is VersionStatus.SUPERSEDED
    current = await platform.get_published(tenant.operator, "emitir_protocolo")
    assert current is not None and current.version_no == 2
    # A execução já enfileirada continua presa à versão 1.
    pinned = await platform.get(tenant.operator, run.run_id)
    assert pinned is not None and pinned.version_no == 1

    async with app_db.tenant_session(tenant.company_id) as session:
        actions = (await session.scalars(text("SELECT action FROM audit_log ORDER BY seq"))).all()
        chain = await verify_chain(session, tenant.company_id)
    # Primeiro o cadastro da empresa (auditado também), depois o ciclo de vida.
    assert len(actions) - len(without_provisioning(actions)) == TENANT_AUDIT_ROWS
    assert actions[0] == "empresa.criada"
    assert without_provisioning(actions) == [
        "robo.versao_criada",
        "robo.versao_validada",
        "robo.versao_publicada",
        "execucao.iniciada",
        "execucao.iniciada",
        "robo.versao_criada",
        "robo.versao_validada",
        "robo.versao_publicada",
    ]
    assert chain.ok, chain.problems


async def test_creator_cannot_approve_own_version(app_db: Database) -> None:
    platform = PgPlatform(app_db)
    tenant = await make_tenant(app_db, "Mesma pessoa")
    provisioning = PgProvisioning(app_db)
    user = await provisioning.create_user(tenant.company_id, f"ambos-{new_id()}")
    for role in (Role.CREATOR, Role.APPROVER):
        await provisioning.add_membership(tenant.company_id, user, role, tenant.area_id)
    async with app_db.tenant_session(tenant.company_id) as session:
        subject = await session.scalar(
            text("SELECT subject FROM users WHERE id = :id"), {"id": user}
        )
    both = await PgPrincipalResolver(app_db).resolve(subject, tenant.company_id)
    assert both is not None
    version = await platform.create_version(both, tenant.area_id, protocolo())
    await platform.mark_validated(both, version.robot_id, 1)
    with pytest.raises(Forbidden):
        await platform.publish(both, version.robot_id, 1)
    info = await platform.get_version(both, version.robot_id, 1)
    assert info is not None and info.status is VersionStatus.VALIDATED


async def test_policy_violations_block_validation(app_db: Database) -> None:
    platform = PgPlatform(app_db)
    tenant = await make_tenant(app_db, "Política")
    broken = emitir_protocolo(BASE, "outro.rapa.test")
    version = await platform.create_version(tenant.creator, tenant.area_id, broken)
    assert version.violations
    with pytest.raises(InvalidInput) as caught:
        await platform.mark_validated(tenant.creator, version.robot_id, 1)
    assert caught.value.errors == version.violations
    with pytest.raises(Conflict):
        await platform.publish(tenant.approver, version.robot_id, 1)


async def test_role_checks(app_db: Database) -> None:
    platform = PgPlatform(app_db)
    tenant = await make_tenant(app_db, "Papéis")
    with pytest.raises(Forbidden):
        await platform.create_version(tenant.viewer, tenant.area_id, protocolo())
    with pytest.raises(Forbidden):
        await platform.create_version(tenant.creator, tenant.other_area_id, protocolo())
    version = await platform.create_version(tenant.creator, tenant.area_id, protocolo())
    with pytest.raises(Forbidden):
        await platform.mark_validated(tenant.operator, version.robot_id, 1)
    await platform.mark_validated(tenant.creator, version.robot_id, 1)
    with pytest.raises(Forbidden):
        await platform.publish(tenant.creator, version.robot_id, 1)
    # Quem só tem papel em outra área nem enxerga o robô.
    with pytest.raises(NotFound):
        await platform.publish(tenant.outsider, version.robot_id, 1)
    assert await platform.get_version(tenant.outsider, version.robot_id, 1) is None
    assert await platform.list_robots(tenant.outsider) == []
    assert await platform.get_version(tenant.creator, version.robot_id, 9) is None
    await platform.publish(tenant.admin, version.robot_id, 1)  # admin da empresa aprova
    assert await platform.list_published(tenant.outsider) == []
    with pytest.raises(NotFound):
        await platform.start(tenant.outsider, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.MCP)
    with pytest.raises(Forbidden):
        await platform.start(tenant.viewer, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.MCP)
    run = await platform.start(tenant.admin, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.MCP)
    assert run.state is RunState.QUEUED


async def test_area_must_exist_in_company(app_db: Database) -> None:
    platform = PgPlatform(app_db)
    tenant = await make_tenant(app_db, "Área A")
    other = await make_tenant(app_db, "Área B")
    admin = tenant.admin
    with pytest.raises(NotFound):
        await platform.create_version(admin, other.area_id, protocolo())
    with pytest.raises(NotFound):
        await platform.create_version(admin, new_id(), protocolo())
    await platform.create_version(admin, tenant.area_id, protocolo())
    with pytest.raises(Conflict):
        await platform.create_version(admin, tenant.other_area_id, protocolo())
    with pytest.raises(NotFound):
        await platform.mark_validated(admin, new_id(), 1)
    robots = await platform.list_robots(admin)
    with pytest.raises(NotFound):
        await platform.mark_validated(admin, robots[0].robot_id, 7)


async def test_input_validation_messages(app_db: Database) -> None:
    platform = PgPlatform(app_db)
    tenant = await make_tenant(app_db, "Entradas")
    await publish(platform, tenant, protocolo())
    with pytest.raises(InvalidInput) as caught:
        await platform.start(
            tenant.operator,
            "emitir_protocolo",
            {"nome": 3, "cpf": "123", "extra": "x"},
            RunTrigger.REST,
        )
    errors = caught.value.errors
    assert "campo obrigatório ausente: uf" in errors
    assert "campo não declarado: extra" in errors
    assert "nome: tipo inválido; esperado texto" in errors
    assert "cpf: valor fora do formato esperado" in errors
    assert all("123" not in e for e in errors)  # o valor recebido não volta na mensagem


async def test_idempotency(app_db: Database, superuser_engine: AsyncEngine) -> None:
    platform = PgPlatform(app_db)
    tenant = await make_tenant(app_db, "Idempotência")
    other = await make_tenant(app_db, "Idempotência B")
    await publish(platform, tenant, protocolo())
    await publish(platform, other, protocolo())
    p = tenant.operator
    a = await platform.start(p, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.MCP, "chave-1")
    b = await platform.start(p, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.MCP, "chave-1")
    assert a.run_id == b.run_id
    with pytest.raises(Conflict):
        await platform.start(
            p, "emitir_protocolo", {**PROTOCOLO_INPUTS, "uf": "RJ"}, RunTrigger.MCP, "chave-1"
        )
    c = await platform.start(p, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.MCP, "chave-2")
    assert c.run_id != a.run_id
    # A mesma chave em outra empresa é outra chave.
    d = await platform.start(
        other.operator, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.MCP, "chave-1"
    )
    assert d.run_id not in {a.run_id, c.run_id}
    # Vencida, a chave vale para uma nova execução.
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE idempotency_keys SET expires_at = now() - interval '1 second' "
                "WHERE company_id = :company AND key = 'chave-1'"
            ),
            {"company": tenant.company_id},
        )
    e = await platform.start(
        p, "emitir_protocolo", {**PROTOCOLO_INPUTS, "uf": "RJ"}, RunTrigger.MCP, "chave-1"
    )
    assert e.run_id != a.run_id
    f = await platform.start(
        p, "emitir_protocolo", {**PROTOCOLO_INPUTS, "uf": "RJ"}, RunTrigger.MCP, "chave-1"
    )
    assert f.run_id == e.run_id
    with pytest.raises(InvalidInput):
        await platform.start(p, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.MCP, "")


async def test_queue_limit_per_company(app_db: Database) -> None:
    platform = PgPlatform(app_db)
    tenant = await make_tenant(app_db, "Fila cheia", max_queued=1)
    await publish(platform, tenant, protocolo())
    first = await platform.start(
        tenant.operator, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.REST, "k"
    )
    with pytest.raises(QuotaExceeded) as caught:
        await platform.start(tenant.operator, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.REST)
    assert caught.value.retry_after_s > 0
    # A repetição idempotente devolve a execução existente mesmo com a fila cheia.
    again = await platform.start(
        tenant.operator, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.REST, "k"
    )
    assert again.run_id == first.run_id


async def test_portuguese_search(app_db: Database) -> None:
    platform = PgPlatform(app_db)
    tenant = await make_tenant(app_db, "Busca")
    await publish(platform, tenant, protocolo())
    await publish(platform, tenant, notas())
    p = tenant.operator
    found = await platform.search(p, "emissão protocolo")
    assert found and found[0].slug == "emitir_protocolo"
    assert next(s.slug for s in await platform.search(p, "NOTAS fiscais")) == "baixar_notas"
    assert [s.slug for s in await platform.search(p, "emitir")] == ["emitir_protocolo"]
    assert [s.slug for s in await platform.search(p, "baix")] == ["baixar_notas"]  # prefixo
    assert [s.slug for s in await platform.search(p, "  ")] == ["baixar_notas", "emitir_protocolo"]
    assert await platform.search(p, "inexistente") == []
    assert len(await platform.search(p, "", limit=1)) == 1
    assert await platform.search(tenant.outsider, "protocolo") == []


async def test_principal_resolver(app_db: Database) -> None:
    tenant = await make_tenant(app_db, "Resolvedor")
    other = await make_tenant(app_db, "Resolvedor B")
    resolver = PgPrincipalResolver(app_db)
    approver = await resolver.resolve(tenant.approver.subject, tenant.company_id)
    assert approver is not None
    assert approver.company_roles == frozenset()
    assert approver.area_roles == {tenant.area_id: frozenset({Role.APPROVER, Role.OPERATOR})}
    assert approver.user_id is not None
    admin = await resolver.resolve(tenant.admin.subject, tenant.company_id)
    assert admin is not None and admin.company_roles == frozenset({Role.COMPANY_ADMIN})
    # O usuário existe, mas em outra empresa: para esta, ele não existe.
    assert await resolver.resolve(tenant.admin.subject, other.company_id) is None
    assert await resolver.resolve("ninguem", tenant.company_id) is None
    async with app_db.tenant_session(tenant.company_id) as session:
        await session.execute(
            text("UPDATE users SET active = false WHERE subject = :s"),
            {"s": tenant.viewer.subject},
        )
    assert await resolver.resolve(tenant.viewer.subject, tenant.company_id) is None


async def test_validation_run_is_interactive_and_stops_before_writing(app_db: Database) -> None:
    platform = PgPlatform(app_db)
    tenant = await make_tenant(app_db, "Validação")
    version = await platform.create_version(tenant.creator, tenant.area_id, protocolo())
    status = await platform.start_validation(tenant.creator, version.robot_id, 1, PROTOCOLO_INPUTS)
    assert status.trigger is RunTrigger.VALIDATION
    with pytest.raises(Forbidden):
        await platform.start_validation(tenant.operator, version.robot_id, 1, PROTOCOLO_INPUTS)
    with pytest.raises(NotFound):
        await platform.start_validation(tenant.outsider, version.robot_id, 1, PROTOCOLO_INPUTS)
    async with app_db.tenant_session(tenant.company_id) as session:
        row = (
            await session.execute(
                text("SELECT mode, priority FROM runs WHERE id = :id"), {"id": status.run_id}
            )
        ).one()
    assert tuple(row) == ("until_first_write", 0)


async def test_list_versions_and_recent_runs_respect_areas(app_db: Database) -> None:
    platform = PgPlatform(app_db)
    tenant = await make_tenant(app_db, "Listas")
    other = await make_tenant(app_db, "Listas B")
    first = await publish(platform, tenant, protocolo())
    await platform.create_version(tenant.creator, tenant.area_id, protocolo())

    versions = await platform.list_versions(tenant.approver, first.robot_id)
    assert [(v.version_no, v.status) for v in versions] == [
        (1, VersionStatus.PUBLISHED),
        (2, VersionStatus.DRAFT),
    ]
    for stranger in (tenant.outsider, other.admin):
        with pytest.raises(NotFound):
            await platform.list_versions(stranger, first.robot_id)

    runs = [
        await platform.start(tenant.operator, "emitir_protocolo", PROTOCOLO_INPUTS, trigger)
        for trigger in (RunTrigger.MCP, RunTrigger.REST, RunTrigger.MANUAL)
    ]
    recent = await platform.list_recent(tenant.operator, 2)
    assert [r.run_id for r in recent] == [runs[2].run_id, runs[1].run_id]
    assert [r.trigger for r in await platform.list_recent(tenant.admin)] == [
        RunTrigger.MANUAL,
        RunTrigger.REST,
        RunTrigger.MCP,
    ]
    # Papel só noutra área (ou noutra empresa): não vê as execuções nem pelo número.
    assert await platform.list_recent(tenant.outsider) == []
    assert await platform.list_recent(other.admin) == []
    assert await platform.get(tenant.outsider, runs[0].run_id) is None
    assert await platform.get(other.admin, runs[0].run_id) is None


async def test_provisioning_is_audited(app_db: Database) -> None:
    """Cada passo do cadastro (``PgProvisioning``) fica na auditoria, com quem pediu."""
    provisioning = PgProvisioning(app_db, requested_by="automação  do cadastro")
    company = await provisioning.create_company("Cadastro auditado", guaranteed_slots=2)
    await provisioning.set_quota(company, guaranteed_slots=3, max_borrow_factor=1.5, max_queued=9)
    area = await provisioning.create_area(company, "Fiscal")
    user = await provisioning.create_user(company, f"ana-{new_id()}@empresa", "Ana")
    await provisioning.add_membership(company, user, Role.PLATFORM_ADMIN)
    await provisioning.add_membership(company, user, Role.CREATOR, area)
    await provisioning.register_credential(
        company,
        area,
        CredentialRef(name="portal", kind="password", origin="https://portal.rapa.test"),
        vault_path=f"empresas/{company}/portal",
    )
    async with app_db.tenant_session(company) as session:
        rows = (
            await session.execute(text("SELECT action, actor, target FROM audit_log ORDER BY seq"))
        ).all()
        chain = await verify_chain(session, company)
    assert chain.ok, chain.problems
    assert [row[0] for row in rows] == [
        "empresa.criada",
        "empresa.cota_alterada",
        "area.criada",
        "usuario.criado",
        "usuario.papeis_alterados",
        "usuario.papeis_alterados",
        "credencial.registrada",
    ]
    assert {row[1] for row in rows} == {"plataforma:automação do cadastro"}
    assert rows[0][2] == f"empresa:{company}" and rows[2][2] == f"area:{area}"
    assert rows[4][2] == rows[5][2] == f"usuario:{user}"
    # A cota de uma empresa que não existe: nada muda, nada é auditado.
    with pytest.raises(NotFound):
        await provisioning.set_quota(
            new_id(), guaranteed_slots=1, max_borrow_factor=2, max_queued=1
        )
