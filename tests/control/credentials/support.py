"""Auxiliares dos testes do certificado A1: o robô com A1, as pessoas e o certificado."""

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from rapa.control.db import Database
from rapa.control.services import PgCredentials, PgPlatform, PgPrincipalResolver, PgProvisioning
from rapa.core.contracts import CredentialInfo, Principal, RobotVersionInfo, Role
from rapa.core.robot import CredentialKind, Manifest
from tests.control.db.support import BASE, Tenant, make_tenant, protocolo

#: A origem do certificado (a do portal de teste, na forma do navegador).
ORIGIN = BASE
CERT = "certificado"
FINGERPRINT = "ab" * 32
OTHER_FINGERPRINT = "cd" * 32


def a1_protocolo(*, origin: str = ORIGIN, name: str = "emitir_protocolo") -> Manifest:
    """O robô de protocolo do portal, declarando também um certificado digital A1."""
    data = protocolo().model_dump(mode="json")
    data["name"] = name
    data["credentials"] = [
        *data["credentials"],
        {"name": CERT, "kind": "pfx_a1", "origin": origin, "description": "Certificado A1"},
    ]
    return Manifest.model_validate(data)


def in_days(days: float) -> datetime:
    return datetime.now(UTC) + timedelta(days=days)


@dataclass(frozen=True)
class A1World:
    """Uma empresa com as pessoas do A1: quem administra a área, quem guarda o
    certificado e uma segunda pessoa que aprova."""

    tenant: Tenant
    area_admin: Principal
    custodian: Principal
    approver2: Principal
    #: Custodiante só da outra área (não serve para esta).
    other_custodian: Principal
    platform: PgPlatform
    credentials: PgCredentials


async def person(
    db: Database, tenant: Tenant, label: str, *grants: tuple[Role, UUID | None]
) -> Principal:
    provisioning = PgProvisioning(db)
    subject = f"{label}-{secrets.token_hex(4)}@a1"
    user_id = await provisioning.create_user(tenant.company_id, subject, label.capitalize())
    for role, area_id in grants:
        await provisioning.add_membership(tenant.company_id, user_id, role, area_id)
    principal = await PgPrincipalResolver(db).resolve(subject, tenant.company_id)
    assert principal is not None
    return principal


async def a1_world(db: Database, name: str) -> A1World:
    tenant = await make_tenant(db, name)
    area = tenant.area_id
    return A1World(
        tenant=tenant,
        area_admin=await person(db, tenant, "administradora", (Role.AREA_ADMIN, area)),
        custodian=await person(db, tenant, "custodiante", (Role.CREDENTIAL_CUSTODIAN, area)),
        approver2=await person(
            db, tenant, "aprovadora", (Role.APPROVER, area), (Role.OPERATOR, area)
        ),
        other_custodian=await person(
            db, tenant, "guarda-juridico", (Role.CREDENTIAL_CUSTODIAN, tenant.other_area_id)
        ),
        platform=PgPlatform(db),
        credentials=PgCredentials(db),
    )


async def register_a1(
    world: A1World,
    *,
    not_after: datetime | None = None,
    fingerprint: str | None = FINGERPRINT,
    origin: str = ORIGIN,
) -> CredentialInfo:
    return await world.credentials.register(
        world.area_admin,
        world.tenant.area_id,
        name=CERT,
        kind=CredentialKind.PFX_A1,
        origin=origin,
        vault_path=f"empresas/{world.tenant.company_id}/certificado-a1",
        custodian_subject=world.custodian.subject,
        not_after=not_after or in_days(365),
        fingerprint=fingerprint,
    )


async def draft_a1(world: A1World, manifest: Manifest | None = None) -> RobotVersionInfo:
    """O rascunho do robô com A1, já validado (pronto para aprovar)."""
    tenant = world.tenant
    version = await world.platform.create_version(
        tenant.creator, tenant.area_id, manifest or a1_protocolo()
    )
    return await world.platform.mark_validated(tenant.creator, version.robot_id, version.version_no)


async def ready_a1(world: A1World) -> RobotVersionInfo:
    """Certificado registrado, versão validada e o robô autorizado pelo custodiante."""
    await register_a1(world)
    version = await draft_a1(world)
    await world.credentials.authorize(world.custodian, version.robot_id, CERT)
    return version


async def publish_a1(world: A1World) -> RobotVersionInfo:
    """O robô com A1 publicado pelas duas pessoas que aprovam."""
    version = await ready_a1(world)
    first = await world.platform.publish(
        world.tenant.approver, version.robot_id, version.version_no
    )
    assert first.approvals == [world.tenant.approver.subject]
    return await world.platform.publish(world.approver2, version.robot_id, version.version_no)


async def set_not_after(superuser: AsyncEngine, credential_id: UUID, when: datetime | None) -> None:
    """Muda a validade direto no banco (um certificado que venceu, ou uma linha antiga)."""
    async with superuser.begin() as conn:
        await conn.execute(
            text("UPDATE credentials SET not_after = :when WHERE id = :id"),
            {"when": when, "id": credential_id},
        )
