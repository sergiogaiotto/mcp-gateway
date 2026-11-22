"""As regras do A1 também no banco: gatilhos, restrições e permissões."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from rapa.control.db import Database
from rapa.control.services import CredentialAlerts
from rapa.core.contracts import Role
from rapa.core.robot import CredentialKind, Manifest
from tests.control.db.support import protocolo

from .support import (
    CERT,
    ORIGIN,
    a1_protocolo,
    a1_world,
    in_days,
    person,
    ready_a1,
    register_a1,
)

pytestmark = pytest.mark.database


@asynccontextmanager
async def as_tenant(engine: AsyncEngine, company_id: UUID) -> AsyncIterator[AsyncConnection]:
    async with engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('rapa.company_id', :id, true)"), {"id": str(company_id)}
        )
        yield conn


async def _refused(engine: AsyncEngine, company_id: UUID, statement: str, **params: Any) -> str:
    with pytest.raises(DBAPIError) as caught:
        async with as_tenant(engine, company_id) as conn:
            await conn.execute(text(statement), params)
    return str(getattr(caught.value.orig, "sqlstate", None))


async def test_publishing_by_sql_needs_the_approvals_the_manifest_asks(
    app_db: Database, owner_engine: AsyncEngine
) -> None:
    world = await a1_world(app_db, "Gatilho das aprovações")
    tenant = world.tenant
    version = await ready_a1(world)
    first = await world.platform.publish(tenant.approver, version.robot_id, 1)
    assert first.approvals == [tenant.approver.subject]
    publish_sql = (
        "UPDATE robot_versions SET status = 'published', approved_by = :who WHERE id = :id"
    )
    # Com uma aprovação só, nem a aplicação nem o dono publicam um robô com A1 por SQL.
    for engine in (app_db.engine, owner_engine):
        state = await _refused(
            engine,
            tenant.company_id,
            publish_sql,
            who=tenant.approver.subject,
            id=version.version_id,
        )
        assert state == "23514"

    # Sem A1, uma aprovação basta, mas quem publica tem que estar entre os aprovadores.
    plain = await world.platform.create_version(
        tenant.creator, tenant.area_id, protocolo().model_copy(update={"name": "protocolo_simples"})
    )
    await world.platform.mark_validated(tenant.creator, plain.robot_id, 1)
    assert (
        await _refused(
            app_db.engine,
            tenant.company_id,
            publish_sql,
            who=tenant.approver.subject,
            id=plain.version_id,
        )
        == "23514"
    )
    async with as_tenant(app_db.engine, tenant.company_id) as conn:
        await conn.execute(
            text(
                "INSERT INTO version_approvals (company_id, robot_id, version_no, approved_by) "
                "VALUES (:c, :r, 1, :who)"
            ),
            {"c": tenant.company_id, "r": plain.robot_id, "who": tenant.approver.subject},
        )
    assert (
        await _refused(
            app_db.engine, tenant.company_id, publish_sql, who="outra-pessoa", id=plain.version_id
        )
        == "23514"
    )
    async with as_tenant(app_db.engine, tenant.company_id) as conn:
        await conn.execute(
            text(publish_sql), {"who": tenant.approver.subject, "id": plain.version_id}
        )


async def test_a_new_version_is_born_a_draft_without_approver(
    app_db: Database, owner_engine: AsyncEngine
) -> None:
    """Incluir a versão já publicada (ou validada, ou com aprovador) pularia as aprovações:
    nem a aplicação nem o dono conseguem."""
    world = await a1_world(app_db, "Versão nova publicada por SQL")
    tenant = world.tenant
    version = await ready_a1(world)
    copy = (
        "INSERT INTO robot_versions (id, company_id, robot_id, version_no, status, manifest, "
        "fingerprint, created_by, approved_by) SELECT gen_random_uuid(), company_id, robot_id, "
        "version_no + 10, :status, manifest, fingerprint, created_by, :who "
        "FROM robot_versions WHERE id = :id"
    )
    for engine in (app_db.engine, owner_engine):
        for status, who in (
            ("published", tenant.approver.subject),
            ("published", None),
            ("validated", None),
            ("draft", tenant.approver.subject),
        ):
            state = await _refused(
                engine, tenant.company_id, copy, status=status, who=who, id=version.version_id
            )
            assert state == "23514", (status, who)
    async with as_tenant(app_db.engine, tenant.company_id) as conn:
        count = await conn.scalar(
            text("SELECT count(*) FROM robot_versions WHERE robot_id = :r"),
            {"r": version.robot_id},
        )
        approvals = await conn.scalar(
            text("SELECT count(*) FROM version_approvals WHERE robot_id = :r"),
            {"r": version.robot_id},
        )
    assert (count, approvals) == (1, 0)
    # Um rascunho novo continua entrando (é o que criar uma versão faz).
    async with as_tenant(app_db.engine, tenant.company_id) as conn:
        await conn.execute(text(copy), {"status": "draft", "who": None, "id": version.version_id})


async def test_custodians_never_approve_and_their_versions_get_no_approvals_by_sql(
    app_db: Database, owner_engine: AsyncEngine
) -> None:
    world = await a1_world(app_db, "Separação de funções no banco")
    tenant = world.tenant
    version = await ready_a1(world)
    insert = (
        "INSERT INTO version_approvals (company_id, robot_id, version_no, approved_by) "
        "VALUES (:c, :r, :n, :who)"
    )
    params = {"c": tenant.company_id, "r": version.robot_id}
    # O custodiante do certificado não aprova, nem por SQL (o banco não olha o papel).
    for engine in (app_db.engine, owner_engine):
        assert (
            await _refused(
                engine, tenant.company_id, insert, **params, n=1, who=world.custodian.subject
            )
            == "23514"
        )
    # Uma versão criada por quem é custodiante de um certificado dele não recebe aprovações.
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
        name="certificado_dois",
        kind=CredentialKind.PFX_A1,
        origin=ORIGIN,
        vault_path="empresas/x/dois",
        custodian_subject=creator_custodian.subject,
        not_after=in_days(100),
    )
    data = a1_protocolo().model_dump(mode="json")
    data["credentials"].append({"name": "certificado_dois", "kind": "pfx_a1", "origin": ORIGIN})
    mine = await world.platform.create_version(
        creator_custodian, tenant.area_id, Manifest.model_validate(data)
    )
    await world.platform.mark_validated(creator_custodian, version.robot_id, mine.version_no)
    for engine in (app_db.engine, owner_engine):
        assert (
            await _refused(
                engine,
                tenant.company_id,
                insert,
                **params,
                n=mine.version_no,
                who=tenant.approver.subject,
            )
            == "23514"
        )
    # A versão de outra pessoa recebe a aprovação de quem não guarda o certificado.
    async with as_tenant(app_db.engine, tenant.company_id) as conn:
        await conn.execute(text(insert), {**params, "n": 1, "who": tenant.approver.subject})


async def test_approvals_are_append_only_and_never_from_the_author(
    app_db: Database, owner_engine: AsyncEngine
) -> None:
    world = await a1_world(app_db, "Aprovações só inclusões")
    tenant = world.tenant
    version = await world.platform.create_version(tenant.creator, tenant.area_id, protocolo())
    insert = (
        "INSERT INTO version_approvals (company_id, robot_id, version_no, approved_by) "
        "VALUES (:c, :r, 1, :who)"
    )
    params = {"c": tenant.company_id, "r": version.robot_id}
    # Um rascunho não recebe aprovações.
    assert (
        await _refused(app_db.engine, tenant.company_id, insert, **params, who="alguem") == "23514"
    )
    await world.platform.mark_validated(tenant.creator, version.robot_id, 1)
    # Quem criou não aprova, nem por SQL.
    assert (
        await _refused(
            app_db.engine, tenant.company_id, insert, **params, who=tenant.creator.subject
        )
        == "23514"
    )
    await world.platform.publish(tenant.approver, version.robot_id, 1)
    for statement in (
        "UPDATE version_approvals SET approved_by = 'outra' WHERE robot_id = :r",
        "DELETE FROM version_approvals WHERE robot_id = :r",
    ):
        assert (
            await _refused(app_db.engine, tenant.company_id, statement, r=version.robot_id)
            == "42501"
        )
        assert (
            await _refused(owner_engine, tenant.company_id, statement, r=version.robot_id)
            == "23514"
        )


async def test_authorizations_only_get_revoked_once(
    app_db: Database, owner_engine: AsyncEngine
) -> None:
    world = await a1_world(app_db, "Autorizações só revogação")
    tenant = world.tenant
    version = await ready_a1(world)
    [authorization] = await world.credentials.list_authorizations(world.custodian, version.robot_id)
    aid = authorization.authorization_id
    # A aplicação não muda outras colunas nem apaga.
    for statement in (
        "UPDATE a1_authorizations SET fingerprint_sha256 = repeat('e', 64) WHERE id = :id",
        "DELETE FROM a1_authorizations WHERE id = :id",
    ):
        assert await _refused(app_db.engine, tenant.company_id, statement, id=aid) == "42501"
    # O dono tem a permissão, mas o gatilho barra.
    for statement in (
        "UPDATE a1_authorizations SET fingerprint_sha256 = repeat('e', 64) WHERE id = :id",
        "UPDATE a1_authorizations SET authorized_by = 'outra' WHERE id = :id",
        "UPDATE a1_authorizations SET created_at = now() WHERE id = :id",
        "DELETE FROM a1_authorizations WHERE id = :id",
    ):
        assert await _refused(owner_engine, tenant.company_id, statement, id=aid) == "23514", (
            statement
        )
    # Uma ativa por robô e credencial.
    duplicate = (
        "INSERT INTO a1_authorizations (company_id, id, area_id, robot_id, credential_id, origin, "
        "authorized_by) SELECT company_id, gen_random_uuid(), area_id, robot_id, credential_id, "
        "origin, 'outra' FROM a1_authorizations WHERE id = :id"
    )
    assert await _refused(app_db.engine, tenant.company_id, duplicate, id=aid) == "23505"
    await world.credentials.revoke_authorization(world.custodian, aid)
    for statement in (
        "UPDATE a1_authorizations SET revoked_at = now(), revoked_by = 'x' WHERE id = :id",
        "UPDATE a1_authorizations SET revoked_at = NULL, revoked_by = NULL WHERE id = :id",
    ):
        assert await _refused(app_db.engine, tenant.company_id, statement, id=aid) == "23514"


async def test_credentials_only_change_validity_fingerprint_and_revocation(
    app_db: Database, owner_engine: AsyncEngine
) -> None:
    world = await a1_world(app_db, "Credenciais fixas")
    tenant = world.tenant
    credential = await register_a1(world)
    cid = credential.credential_id
    for statement in (
        "UPDATE credentials SET vault_path = 'outro/lugar' WHERE id = :id",
        "UPDATE credentials SET origin = 'https://outro.rapa.test' WHERE id = :id",
        "DELETE FROM credentials WHERE id = :id",
    ):
        assert await _refused(app_db.engine, tenant.company_id, statement, id=cid) == "42501"
    for statement in (
        "UPDATE credentials SET vault_path = 'outro/lugar' WHERE id = :id",
        "UPDATE credentials SET name = 'outro' WHERE id = :id",
        "UPDATE credentials SET custodian_user_id = NULL WHERE id = :id",
        "UPDATE credentials SET fingerprint_sha256 = 'XYZ' WHERE id = :id",
        "UPDATE credentials SET not_after = NULL WHERE id = :id",
        "UPDATE credentials SET revoked_at = now() WHERE id = :id",
    ):
        assert await _refused(owner_engine, tenant.company_id, statement, id=cid) == "23514", (
            statement
        )
    await world.credentials.revoke(world.custodian, cid)
    assert (
        await _refused(
            app_db.engine,
            tenant.company_id,
            "UPDATE credentials SET revoked_at = NULL, revoked_by = NULL WHERE id = :id",
            id=cid,
        )
        == "23514"
    )


async def test_expiry_alerts_are_append_only(
    app_db: Database, dispatcher_db: Database, owner_engine: AsyncEngine
) -> None:
    world = await a1_world(app_db, "Avisos só inclusões")
    credential = await register_a1(world, not_after=in_days(3))
    assert await CredentialAlerts(dispatcher_db).check() >= 1
    for statement in (
        "UPDATE credential_alerts SET kind = 'vencida' WHERE credential_id = :id",
        "DELETE FROM credential_alerts WHERE credential_id = :id",
    ):
        assert (
            await _refused(
                owner_engine, world.tenant.company_id, statement, id=credential.credential_id
            )
            == "23514"
        )
    assert CERT == credential.name
