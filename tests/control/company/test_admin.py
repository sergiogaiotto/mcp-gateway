"""Administração da empresa por ela mesma: usuários, papéis e áreas (PgCompanyAdmin)."""

import asyncio
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from rapa.control.db import Database
from rapa.control.services import PgCompanyAdmin, PgPrincipalResolver, PgProvisioning
from rapa.control.services.lifecycle import unfreeze_company
from rapa.core.contracts import (
    CompanyFrozen,
    CompanyStatus,
    Conflict,
    Forbidden,
    InvalidInput,
    NotFound,
    Principal,
    Role,
    RoleGrant,
)
from rapa.fixtures.memory import make_principal
from tests.control.db.support import Tenant, make_tenant

from .support import audit_actions, freeze, person

pytestmark = pytest.mark.database


@dataclass
class People:
    a: Tenant
    b: Tenant
    area_admin: Principal  # administra a área Fiscal
    auditor: Principal
    service: PgCompanyAdmin


@pytest.fixture
async def people(app_db: Database) -> AsyncIterator[People]:
    a = await make_tenant(app_db, "Empresa A")
    b = await make_tenant(app_db, "Empresa B")
    tag = secrets.token_hex(4)
    area_admin = await person(app_db, a.company_id, f"chefe-{tag}@a", (Role.AREA_ADMIN, a.area_id))
    auditor = await person(app_db, a.company_id, f"auditora-{tag}@a", (Role.AUDITOR, None))
    yield People(a, b, area_admin, auditor, PgCompanyAdmin(app_db))


def _grants(user: object) -> set[tuple[Role, object]]:
    return {(g.role, g.area_id) for g in user.roles}  # type: ignore[attr-defined]


async def test_company_admin_lists_and_changes_roles_anywhere(
    people: People, app_db: Database, superuser_engine: AsyncEngine
) -> None:
    a, service = people.a, people.service
    users = await service.list_users(a.admin)
    by_subject = {u.subject: u for u in users}
    assert {a.operator.subject, a.admin.subject, people.area_admin.subject} <= set(by_subject)
    operator = by_subject[a.operator.subject]
    assert _grants(operator) == {(Role.OPERATOR, a.area_id)}
    # A lista inteira, no escopo de quem administra a empresa: dá e tira de uma vez.
    changed = await service.set_roles(
        a.admin,
        operator.user_id,
        [
            RoleGrant(role=Role.CREATOR, area_id=a.other_area_id),
            RoleGrant(role=Role.VIEWER, area_id=None),
        ],
    )
    assert _grants(changed) == {(Role.CREATOR, a.other_area_id), (Role.VIEWER, None)}
    resolved = await PgPrincipalResolver(app_db).resolve(a.operator.subject, a.company_id)
    assert resolved is not None
    assert resolved.company_roles == {Role.VIEWER}
    assert resolved.area_roles == {a.other_area_id: {Role.CREATOR}}
    assert "usuario.papeis_alterados" in await audit_actions(superuser_engine, a.company_id)
    # A mesma lista de novo não muda nada (nem audita).
    count = (await audit_actions(superuser_engine, a.company_id)).count("usuario.papeis_alterados")
    await service.set_roles(
        a.admin,
        operator.user_id,
        [
            RoleGrant(role=Role.VIEWER, area_id=None),
            RoleGrant(role=Role.CREATOR, area_id=a.other_area_id),
        ],
    )
    after = (await audit_actions(superuser_engine, a.company_id)).count("usuario.papeis_alterados")
    assert after == count


async def test_area_admin_only_touches_the_roles_of_his_areas(people: People) -> None:
    a, service, boss = people.a, people.service, people.area_admin
    outsider = next(u for u in await service.list_users(a.admin) if u.subject == a.outsider.subject)
    # O vizinho tem papéis só na outra área: o administrador da Fiscal não os vê.
    seen = next(u for u in await service.list_users(boss) if u.subject == a.outsider.subject)
    assert seen.roles == []
    given = await service.set_roles(
        boss, outsider.user_id, [RoleGrant(role=Role.OPERATOR, area_id=a.area_id)]
    )
    assert _grants(given) == {(Role.OPERATOR, a.area_id)}
    # Os papéis da outra área ficaram (vistos por quem administra a empresa).
    full = next(u for u in await service.list_users(a.admin) if u.subject == a.outsider.subject)
    assert _grants(full) == {
        (Role.OPERATOR, a.area_id),
        (Role.CREATOR, a.other_area_id),
        (Role.OPERATOR, a.other_area_id),
    }
    refused = [
        RoleGrant(role=Role.OPERATOR, area_id=a.other_area_id),  # outra área
        RoleGrant(role=Role.OPERATOR, area_id=None),  # a empresa inteira
        RoleGrant(role=Role.COMPANY_ADMIN, area_id=None),
        RoleGrant(role=Role.PLATFORM_ADMIN, area_id=a.area_id),
    ]
    for grant in refused:
        with pytest.raises(Forbidden):
            await service.set_roles(boss, outsider.user_id, [grant])
    # Quem administra só uma área também não desativa ninguém nem mexe nas áreas.
    with pytest.raises(Forbidden):
        await service.deactivate_user(boss, outsider.user_id)
    with pytest.raises(Forbidden):
        await service.create_area(boss, "Compras")
    with pytest.raises(Forbidden):
        await service.rename_area(boss, a.area_id, "Fiscal 2")
    # Sem papel de administração, nada disso.
    with pytest.raises(Forbidden):
        await service.list_users(a.operator)
    with pytest.raises(Forbidden):
        await service.set_roles(a.operator, outsider.user_id, [])


async def test_platform_admin_is_never_given_nor_taken_here(
    people: People, app_db: Database
) -> None:
    a, service = people.a, people.service
    operator = next(u for u in await service.list_users(a.admin) if u.subject == a.operator.subject)
    with pytest.raises(Forbidden):
        await service.set_roles(
            a.admin, operator.user_id, [RoleGrant(role=Role.PLATFORM_ADMIN, area_id=None)]
        )
    with pytest.raises(InvalidInput):
        await service.set_roles(
            a.admin, operator.user_id, [RoleGrant(role=Role.COMPANY_ADMIN, area_id=a.area_id)]
        )
    # O platform_admin que a plataforma deu fica, mesmo com a lista vazia.
    await PgProvisioning(app_db).add_membership(
        a.company_id, operator.user_id, Role.PLATFORM_ADMIN, None
    )
    emptied = await service.set_roles(a.admin, operator.user_id, [])
    assert _grants(emptied) == {(Role.PLATFORM_ADMIN, None)}


async def test_last_active_company_admin_stays(people: People, app_db: Database) -> None:
    a, service = people.a, people.service
    admin = next(u for u in await service.list_users(a.admin) if u.subject == a.admin.subject)
    with pytest.raises(Conflict) as caught:
        await service.set_roles(a.admin, admin.user_id, [])
    assert "pelo menos um administrador ativo" in caught.value.message
    # Com outra pessoa administrando, dá para tirar.
    second = await person(
        app_db, a.company_id, f"segunda-{secrets.token_hex(4)}@a", (Role.COMPANY_ADMIN, None)
    )
    await service.set_roles(second, admin.user_id, [])
    # Agora a segunda é a última: nem tirando o papel, nem desativando (por alguém de fora
    # do banco, com o papel na mão: a regra é do banco, não de quem pede).
    other = make_principal(a.company_id, Role.COMPANY_ADMIN, subject="plataforma@fora")
    with pytest.raises(Conflict):
        await service.set_roles(other, second.user_id, [])  # type: ignore[arg-type]
    with pytest.raises(Conflict):
        await service.deactivate_user(other, second.user_id)  # type: ignore[arg-type]
    # Um administrador desativado não conta como ativo.
    third = await person(
        app_db, a.company_id, f"terceira-{secrets.token_hex(4)}@a", (Role.COMPANY_ADMIN, None)
    )
    await service.deactivate_user(second, third.user_id)  # type: ignore[arg-type]
    with pytest.raises(Conflict):
        await service.set_roles(other, second.user_id, [])  # type: ignore[arg-type]


async def test_database_keeps_the_last_active_company_admin(
    people: People, app_db: Database
) -> None:
    """Direto no banco, sem o serviço: o último company_admin ativo também fica."""
    a = people.a
    admin, operator = a.admin.user_id, a.operator.user_id

    async def attempt(statement: str, **params: object) -> str | None:
        try:
            async with app_db.tenant_session(a.company_id) as session:
                await session.execute(text(statement), params)
        except DBAPIError as exc:
            return getattr(exc.orig, "sqlstate", None)
        return None

    take = "DELETE FROM memberships WHERE user_id = :u AND role = 'company_admin'"
    for statement in (
        take,
        "UPDATE memberships SET role = 'operator' WHERE user_id = :u AND role = 'company_admin'",
        "UPDATE memberships SET area_id = :area WHERE user_id = :u AND role = 'company_admin'",
        "UPDATE users SET active = false WHERE id = :u",
        "DELETE FROM users WHERE id = :u",
    ):
        assert await attempt(statement, u=admin, area=a.area_id) == "23514", statement
    resolved = await PgPrincipalResolver(app_db).resolve(a.admin.subject, a.company_id)
    assert resolved is not None and resolved.company_roles == {Role.COMPANY_ADMIN}
    # Quem não administra a empresa sai à vontade; o mesmo papel regravado também passa.
    assert await attempt("UPDATE users SET active = false WHERE id = :u", u=operator) is None
    assert (
        await attempt(
            "UPDATE memberships SET created_at = now() WHERE user_id = :u "
            "AND role = 'company_admin'",
            u=admin,
        )
        is None
    )
    # Com uma segunda administradora, dá para tirar uma; ao mesmo tempo, as duas não.
    second = await person(
        app_db, a.company_id, f"segunda-{secrets.token_hex(4)}@a", (Role.COMPANY_ADMIN, None)
    )
    with pytest.raises(DBAPIError) as caught:
        async with app_db.tenant_session(a.company_id) as first:
            await first.execute(text(take), {"u": admin})
            async with app_db.tenant_session(a.company_id) as other:
                await other.execute(text(take), {"u": second.user_id})
            # A outra confirmou antes (ainda via a primeira ativa); esta fica sem nenhuma.
    assert getattr(caught.value.orig, "sqlstate", None) == "23514"
    resolver = PgPrincipalResolver(app_db)
    assert (await resolver.resolve(a.admin.subject, a.company_id)).company_roles == {  # type: ignore[union-attr]
        Role.COMPANY_ADMIN
    }
    assert (await resolver.resolve(second.subject, a.company_id)).company_roles == frozenset()  # type: ignore[union-attr]
    # A conferência toma a trava das mudanças de papéis da empresa (a mesma do serviço):
    # com ela presa, até uma retirada que deixa outro administrador espera.
    third = await person(
        app_db, a.company_id, f"terceira-{secrets.token_hex(4)}@a", (Role.COMPANY_ADMIN, None)
    )
    async with app_db.tenant_session(a.company_id) as holder:
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(7131, hashtext(:c))"), {"c": str(a.company_id)}
        )
        removing = asyncio.create_task(attempt(take, u=third.user_id))
        await asyncio.sleep(0.3)
        assert not removing.done()
    assert await removing is None


async def test_deactivate_and_reactivate(
    people: People, app_db: Database, superuser_engine: AsyncEngine
) -> None:
    a, service = people.a, people.service
    resolver = PgPrincipalResolver(app_db)
    operator = next(u for u in await service.list_users(a.admin) if u.subject == a.operator.subject)
    admin = next(u for u in await service.list_users(a.admin) if u.subject == a.admin.subject)
    with pytest.raises(Conflict) as caught:
        await service.deactivate_user(a.admin, admin.user_id)
    assert "a si mesmo" in caught.value.message
    off = await service.deactivate_user(a.admin, operator.user_id)
    assert not off.active
    assert await resolver.resolve(a.operator.subject, a.company_id) is None
    on = await service.reactivate_user(a.admin, operator.user_id)
    assert on.active
    assert await resolver.resolve(a.operator.subject, a.company_id) is not None
    actions = await audit_actions(superuser_engine, a.company_id)
    assert "usuario.desativado" in actions and "usuario.reativado" in actions
    # Usuário de outra empresa não existe aqui.
    stranger = next(u for u in await service.list_users(people.b.admin))
    for call in (
        service.deactivate_user(a.admin, stranger.user_id),
        service.set_roles(a.admin, stranger.user_id, []),
    ):
        with pytest.raises(NotFound):
            await call


async def test_areas_are_created_and_renamed_by_the_company_admin(
    people: People, superuser_engine: AsyncEngine
) -> None:
    a, service = people.a, people.service
    created = await service.create_area(a.admin, "  Compras   e  Contratos ")
    assert created.name == "Compras e Contratos"
    with pytest.raises(Conflict):
        await service.create_area(a.admin, "Compras e Contratos")
    with pytest.raises(InvalidInput):
        await service.create_area(a.admin, "   ")
    renamed = await service.rename_area(a.admin, created.area_id, "Compras")
    assert renamed.name == "Compras"
    with pytest.raises(Conflict):
        await service.rename_area(a.admin, created.area_id, "Fiscal")
    with pytest.raises(NotFound):
        await service.rename_area(a.admin, people.b.area_id, "Minha")
    names = {area.name for area in await service.list_areas(a.admin)}
    assert names == {"Fiscal", "Jurídico", "Compras"}
    # Quem só tem papel numa área vê só ela.
    assert [area.area_id for area in await service.list_areas(a.operator)] == [a.area_id]
    actions = await audit_actions(superuser_engine, a.company_id)
    assert "area.criada" in actions and "area.renomeada" in actions


async def test_frozen_company_only_reduces_access(
    people: People, owner_engine: AsyncEngine
) -> None:
    a, service = people.a, people.service
    operator = next(u for u in await service.list_users(a.admin) if u.subject == a.operator.subject)
    await freeze(owner_engine, a.company_id, "auditoria de saída")
    info = await service.get_company(a.admin)
    assert info.status is CompanyStatus.FROZEN and info.status_reason == "auditoria de saída"
    # Quem não administra nem audita vê a situação, mas não o motivo.
    plain = await service.get_company(a.operator)
    assert plain.status is CompanyStatus.FROZEN and plain.status_reason is None
    with pytest.raises(CompanyFrozen):
        await service.set_roles(
            a.admin, operator.user_id, [RoleGrant(role=Role.CREATOR, area_id=a.area_id)]
        )
    with pytest.raises(CompanyFrozen):
        await service.create_area(a.admin, "Nova")
    with pytest.raises(CompanyFrozen):
        await service.rename_area(a.admin, a.area_id, "Fiscal 2")
    # Tirar papéis e desativar continuam valendo (só diminuem o acesso).
    emptied = await service.set_roles(a.admin, operator.user_id, [])
    assert emptied.roles == []
    off = await service.deactivate_user(a.admin, operator.user_id)
    assert not off.active
    with pytest.raises(CompanyFrozen):
        await service.reactivate_user(a.admin, operator.user_id)
    await unfreeze_company(owner_engine, a.company_id, requested_by="teste")
    assert (await service.reactivate_user(a.admin, operator.user_id)).active
