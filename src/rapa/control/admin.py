"""Cadastro básico pela linha de comando: empresas, áreas e usuários com papéis.

Uso (a URL é a do papel da aplicação; padrão: ``RAPA_DATABASE_URL``)::

    python -m rapa.control.admin empresa "Nome da empresa" [--vagas 2]
    python -m rapa.control.admin area EMPRESA "Fiscal"
    python -m rapa.control.admin usuario EMPRESA SUJEITO [--nome "Ana Souza"] \\
        [--papel operator] [--papel creator:AREA] ...

Cada comando imprime o identificador criado. ``SUJEITO`` é o ``sub`` do token
do Keycloak (ou o sujeito de um token de desenvolvimento). Um papel sem área
vale na empresa toda; ``papel:AREA`` vale só naquela área.
"""

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy.exc import DBAPIError, IntegrityError

from rapa.control.db import Database
from rapa.control.services import PgProvisioning
from rapa.core.contracts import Role

__all__ = ["main", "parse_role"]


def parse_role(value: str) -> tuple[Role, UUID | None]:
    """``operator`` ou ``creator:<uuid da área>``."""
    name, _, area = value.partition(":")
    try:
        role = Role(name.strip())
    except ValueError:
        valid = ", ".join(r.value for r in Role)
        raise argparse.ArgumentTypeError(f"papel desconhecido: {name!r} (use {valid})") from None
    if not area:
        return role, None
    try:
        return role, UUID(area.strip())
    except ValueError:
        raise argparse.ArgumentTypeError(f"área inválida em {value!r}") from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m rapa.control.admin", description="Cadastro básico do RaPA."
    )
    parser.add_argument("--url", default=None, help="banco (padrão: RAPA_DATABASE_URL)")
    commands = parser.add_subparsers(dest="command", required=True)

    company = commands.add_parser("empresa", help="cadastra uma empresa")
    company.add_argument("nome")
    company.add_argument("--vagas", type=int, default=1, help="execuções garantidas ao mesmo tempo")
    company.add_argument("--fila", type=int, default=1000, help="máximo de execuções na fila")

    area = commands.add_parser("area", help="cadastra uma área numa empresa")
    area.add_argument("empresa", type=UUID)
    area.add_argument("nome")

    user = commands.add_parser("usuario", help="cadastra um usuário e dá papéis")
    user.add_argument("empresa", type=UUID)
    user.add_argument("sujeito")
    user.add_argument("--nome", default="")
    user.add_argument(
        "--papel",
        action="append",
        type=parse_role,
        default=[],
        help="papel na empresa (operator) ou numa área (creator:AREA); pode repetir",
    )
    return parser


async def _run(args: argparse.Namespace, url: str) -> UUID:
    db = Database.from_url(url)
    try:
        provisioning = PgProvisioning(db)
        if args.command == "empresa":
            return await provisioning.create_company(
                args.nome, guaranteed_slots=args.vagas, max_queued=args.fila
            )
        if args.command == "area":
            return await provisioning.create_area(args.empresa, args.nome)
        user_id = await provisioning.create_user(args.empresa, args.sujeito, args.nome)
        for role, area_id in args.papel:
            await provisioning.add_membership(args.empresa, user_id, role, area_id)
        return user_id
    finally:
        await db.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    url = args.url or os.environ.get("RAPA_DATABASE_URL")
    if not url:
        parser.error("informe --url ou defina RAPA_DATABASE_URL")
    try:
        created = asyncio.run(_run(args, url))
    except IntegrityError:
        print(
            "rapa.control.admin: já existe ou não confere (empresa, área ou usuário)",
            file=sys.stderr,
        )
        return 1
    except DBAPIError as exc:
        print(f"rapa.control.admin: erro do banco ({type(exc.orig).__name__})", file=sys.stderr)
        return 1
    print(created)
    return 0


if __name__ == "__main__":
    sys.exit(main())
