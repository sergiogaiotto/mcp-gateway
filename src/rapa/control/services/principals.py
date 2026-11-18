"""Montagem do principal a partir dos vínculos guardados no banco.

Implementa, pela forma (sem importar), o protocolo
``rapa.control.auth.PrincipalResolver``: a camada HTTP valida o token, tira dele
o ``subject`` e a empresa, e pede aqui os papéis. A consulta roda fixada na
empresa do token, então um usuário de outra empresa simplesmente não existe.
"""

from uuid import UUID

from sqlalchemy import select

from rapa.control.db.schema import memberships, users
from rapa.control.services._common import PgService
from rapa.core.contracts import Principal, Role

__all__ = ["PgPrincipalResolver"]


class PgPrincipalResolver(PgService):
    """Papéis da empresa e das áreas de um usuário ativo."""

    async def resolve(self, subject: str, company_id: UUID) -> Principal | None:
        async with self.db.tenant_session(company_id) as session:
            user = (
                (
                    await session.execute(
                        select(users.c.id, users.c.active).where(
                            users.c.subject == subject, users.c.company_id == company_id
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if user is None or not user["active"]:
                return None
            rows = (
                await session.execute(
                    select(memberships.c.area_id, memberships.c.role).where(
                        memberships.c.user_id == user["id"],
                        memberships.c.company_id == company_id,
                    )
                )
            ).all()
        company_roles: set[Role] = set()
        area_roles: dict[UUID, set[Role]] = {}
        for area_id, role in rows:
            if area_id is None:
                company_roles.add(Role(role))
            else:
                area_roles.setdefault(area_id, set()).add(Role(role))
        return Principal(
            subject=subject,
            company_id=company_id,
            user_id=user["id"],
            company_roles=frozenset(company_roles),
            area_roles={area: frozenset(roles) for area, roles in area_roles.items()},
        )
