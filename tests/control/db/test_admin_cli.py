"""Linha de comando de cadastro (python -m rapa.control.admin)."""

import argparse
import asyncio
from uuid import UUID

import pytest

from rapa.control.admin import main, parse_role
from rapa.control.db import Database
from rapa.control.services import PgPrincipalResolver
from rapa.core.contracts import Role
from rapa.core.ids import new_id

from .support import PgDatabase

pytestmark = pytest.mark.database


def _created(capsys: pytest.CaptureFixture[str], argv: list[str]) -> UUID:
    assert main(argv) == 0
    return UUID(capsys.readouterr().out.strip())


async def _resolve(url: str, subject: str, company: UUID) -> object:
    db = Database.from_url(url)
    try:
        return await PgPrincipalResolver(db).resolve(subject, company)
    finally:
        await db.dispose()


def test_company_area_and_user_with_roles(
    pg_database: PgDatabase, capsys: pytest.CaptureFixture[str]
) -> None:
    url = ["--url", pg_database.app_url]
    company = _created(capsys, [*url, "empresa", "Empresa da linha de comando", "--vagas", "2"])
    area = _created(capsys, [*url, "area", str(company), "Fiscal"])
    _created(
        capsys,
        [
            *url,
            "usuario",
            str(company),
            "ana@empresa",
            "--nome",
            "Ana",
            "--papel",
            "operator",
            "--papel",
            f"creator:{area}",
        ],
    )
    principal = asyncio.run(_resolve(pg_database.app_url, "ana@empresa", company))
    assert principal is not None
    assert principal.company_roles == {Role.OPERATOR}  # type: ignore[attr-defined]
    assert principal.area_roles == {area: {Role.CREATOR}}  # type: ignore[attr-defined]

    # Repetir o mesmo usuário na mesma empresa é recusado sem rastro de exceção.
    assert main([*url, "usuario", str(company), "ana@empresa"]) == 1
    assert "já existe" in capsys.readouterr().err
    # Área de outra empresa não vale.
    assert main([*url, "area", str(new_id()), "Compras"]) == 1


def test_role_parsing() -> None:
    area = new_id()
    assert parse_role("approver") == (Role.APPROVER, None)
    assert parse_role(f"creator:{area}") == (Role.CREATOR, area)
    for bad in ("chefe", "creator:nao-e-uuid"):
        with pytest.raises(argparse.ArgumentTypeError):
            parse_role(bad)


def test_url_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RAPA_DATABASE_URL", raising=False)
    with pytest.raises(SystemExit):
        main(["empresa", "Sem banco"])
