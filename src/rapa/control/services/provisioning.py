"""Cadastro de empresas, áreas, usuários, vínculos e credenciais.

São operações de plataforma (entrada de empresa, convite pelo administrador),
chamadas pela automação de cadastro e pelos testes, nunca com dados escolhidos
por quem usa a API: por isso recebem a empresa explicitamente. Cada função roda
numa transação fixada na empresa, então o isolamento por linha continua valendo.

A capacidade da plataforma (``platform_settings``) só muda pelo dono das
tabelas: nem a aplicação nem o despachante podem alterá-la.
"""

from decimal import Decimal
from uuid import UUID

from sqlalchemy import insert, update
from sqlalchemy.ext.asyncio import AsyncEngine

from rapa.control.db.schema import (
    areas,
    companies,
    company_quotas,
    credentials,
    memberships,
    platform_settings,
    users,
)
from rapa.control.services._common import PgService
from rapa.core.contracts import Role
from rapa.core.ids import new_id
from rapa.core.robot import CredentialKind, CredentialRef

__all__ = ["PgProvisioning", "set_platform_capacity"]


class PgProvisioning(PgService):
    """Cadastro básico de uma empresa (Fase 0)."""

    async def create_company(
        self,
        name: str,
        *,
        guaranteed_slots: int = 1,
        max_borrow_factor: Decimal | float = Decimal("2.0"),
        max_queued: int = 1000,
    ) -> UUID:
        company_id = new_id()
        async with self.db.tenant_session(company_id) as session:
            await session.execute(insert(companies).values(id=company_id, name=name))
            await session.execute(
                insert(company_quotas).values(
                    company_id=company_id,
                    guaranteed_slots=guaranteed_slots,
                    max_borrow_factor=Decimal(str(max_borrow_factor)),
                    max_queued=max_queued,
                )
            )
        return company_id

    async def set_quota(
        self,
        company_id: UUID,
        *,
        guaranteed_slots: int,
        max_borrow_factor: Decimal | float,
        max_queued: int,
    ) -> None:
        async with self.db.tenant_session(company_id) as session:
            await session.execute(
                update(company_quotas)
                .where(company_quotas.c.company_id == company_id)
                .values(
                    guaranteed_slots=guaranteed_slots,
                    max_borrow_factor=Decimal(str(max_borrow_factor)),
                    max_queued=max_queued,
                )
            )

    async def create_area(self, company_id: UUID, name: str) -> UUID:
        area_id = new_id()
        async with self.db.tenant_session(company_id) as session:
            await session.execute(
                insert(areas).values(id=area_id, company_id=company_id, name=name)
            )
        return area_id

    async def create_user(self, company_id: UUID, subject: str, display_name: str = "") -> UUID:
        user_id = new_id()
        async with self.db.tenant_session(company_id) as session:
            await session.execute(
                insert(users).values(
                    id=user_id, company_id=company_id, subject=subject, display_name=display_name
                )
            )
        return user_id

    async def add_membership(
        self, company_id: UUID, user_id: UUID, role: Role, area_id: UUID | None = None
    ) -> UUID:
        """Dá um papel ao usuário na empresa toda (``area_id=None``) ou numa área."""
        membership_id = new_id()
        async with self.db.tenant_session(company_id) as session:
            await session.execute(
                insert(memberships).values(
                    id=membership_id,
                    company_id=company_id,
                    area_id=area_id,
                    user_id=user_id,
                    role=role.value,
                )
            )
        return membership_id

    async def register_credential(
        self,
        company_id: UUID,
        area_id: UUID,
        ref: CredentialRef,
        vault_path: str,
        custodian_user_id: UUID | None = None,
    ) -> UUID:
        """Registra a referência a uma credencial do cofre; o valor nunca passa por aqui."""
        credential_id = new_id()
        async with self.db.tenant_session(company_id) as session:
            await session.execute(
                insert(credentials).values(
                    id=credential_id,
                    company_id=company_id,
                    area_id=area_id,
                    name=ref.name,
                    kind=CredentialKind(ref.kind).value,
                    origin=ref.origin,
                    vault_path=vault_path,
                    custodian_user_id=custodian_user_id,
                )
            )
        return credential_id


async def set_platform_capacity(
    owner_engine: AsyncEngine,
    *,
    total_browser_slots: int,
    interactive_reserve_ratio: Decimal | float = Decimal("0.10"),
) -> None:
    """Ajusta a capacidade da plataforma; exige o papel dono das tabelas."""
    async with owner_engine.begin() as conn:
        await conn.execute(
            update(platform_settings)
            .where(platform_settings.c.id.is_(True))
            .values(
                total_browser_slots=total_browser_slots,
                interactive_reserve_ratio=Decimal(str(interactive_reserve_ratio)),
            )
        )
