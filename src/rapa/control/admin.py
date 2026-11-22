"""Cadastro e operação das empresas pela linha de comando.

Entrada e cadastro (a URL é a do papel da aplicação; padrão: ``RAPA_DATABASE_URL``)::

    python -m rapa.control.admin empresa criar "Nome da empresa" --organizacao ORG \\
        --admin SUJEITO [--nome-admin "Ana Souza"] [--area "Fiscal"]... \\
        [--vagas 2] [--fila 1000] [--emprestimo 2.0]
    python -m rapa.control.admin empresa consumo EMPRESA --mes 2026-11 [--json]

Saída (operação da plataforma: a URL é a do dono das tabelas, em ``--url-dono`` ou
``RAPA_OWNER_DATABASE_URL``)::

    python -m rapa.control.admin empresa congelar EMPRESA --motivo "contrato encerrado"
    python -m rapa.control.admin empresa descongelar EMPRESA
    python -m rapa.control.admin empresa exportar EMPRESA --saida empresa.tar.gz
    python -m rapa.control.admin empresa apagar EMPRESA --confirmar "Nome da empresa" \\
        [--segredos-fora-do-openbao]

Os comandos antigos continuam valendo (sem o vínculo com a organização; auditados, como
os novos)::

    python -m rapa.control.admin empresa "Nome da empresa" [--vagas 2]
    python -m rapa.control.admin area EMPRESA "Fiscal"
    python -m rapa.control.admin usuario EMPRESA SUJEITO [--nome "Ana Souza"] \\
        [--papel operator] [--papel creator:AREA] ...

Cada comando de cadastro imprime o identificador criado. ``SUJEITO`` é o ``sub`` do token
do Keycloak (ou o sujeito de um token de desenvolvimento); ``ORG`` é o id ou o apelido
da organização no Keycloak. Um papel sem área vale na empresa toda; ``papel:AREA`` vale
só naquela área. ``--por`` diz quem pediu (vai para a auditoria; padrão: o usuário do
sistema). As pastas dos arquivos e dos artefatos vêm de ``--pasta-arquivos`` e
``--pasta-artefatos`` (padrão: ``RAPA_PASTA_ARQUIVOS`` e ``RAPA_PASTA_ARTEFATOS``). O
armazenamento dos artefatos, as chaves e o cofre vêm das mesmas variáveis do servidor
(``RAPA_ARMAZENAMENTO``, ``RAPA_S3_*``, ``RAPA_CHAVES``, ``RAPA_COFRE``, ``RAPA_BAO_*``;
:mod:`rapa.control.backends`): no S3, ``exportar`` leva os artefatos decifrados e
``apagar`` apaga os objetos e, com o OpenBao, os segredos e a chave transit da empresa.
``apagar`` falha fechado: se a empresa tem artefatos no S3, chaves do transit ou
credenciais e a configuração não alcança onde eles estão, nada é apagado (os segredos de
credenciais fora do OpenBao, só com ``--segredos-fora-do-openbao`` ou ``RAPA_COFRE=dev``).
"""

import argparse
import asyncio
import getpass
import json
import os
import sys
import tempfile
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy.exc import DBAPIError, IntegrityError

from rapa.control.backends import Backends, BackendSettings, VaultKind
from rapa.control.db import Database, create_engine
from rapa.control.services import PgProvisioning
from rapa.control.services.company import company_usage, month_period
from rapa.control.services.lifecycle import (
    create_company,
    export_company,
    freeze_company,
    purge_company,
    unfreeze_company,
)
from rapa.core.contracts import ContractError, InvalidInput, Role, UsageReport

__all__ = ["COMPANY_ACTIONS", "main", "parse_role"]

#: As ações novas de ``empresa`` (``empresa NOME`` continua cadastrando, como antes).
COMPANY_ACTIONS = ("criar", "consumo", "congelar", "descongelar", "exportar", "apagar")
_OWNER_ACTIONS = frozenset({"congelar", "descongelar", "exportar", "apagar"})


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


def _factor(value: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"fator inválido: {value!r}") from None


def _default_user() -> str:
    try:
        return getpass.getuser()
    except KeyError, OSError:
        return "desconhecido"


def _owner_options(command: argparse.ArgumentParser, *, folders: bool = False) -> None:
    command.add_argument(
        "--url-dono",
        default=None,
        help="banco do dono das tabelas (padrão: RAPA_OWNER_DATABASE_URL)",
    )
    command.add_argument("--por", default=None, help="quem pediu (vai para a auditoria)")
    if folders:
        command.add_argument("--pasta-arquivos", type=Path, default=None)
        command.add_argument("--pasta-artefatos", type=Path, default=None)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m rapa.control.admin",
        description="Cadastro e operação das empresas do RaPA.",
    )
    parser.add_argument("--url", default=None, help="banco (padrão: RAPA_DATABASE_URL)")
    commands = parser.add_subparsers(dest="command", required=True)

    company = commands.add_parser(
        "empresa",
        help="cadastra uma empresa (forma antiga; veja também 'empresa criar')",
        epilog="Ações: empresa criar | consumo | congelar | descongelar | exportar | apagar.",
    )
    company.add_argument("nome")
    company.add_argument("--vagas", type=int, default=1, help="execuções garantidas ao mesmo tempo")
    company.add_argument("--fila", type=int, default=1000, help="máximo de execuções na fila")
    company.add_argument("--por", default=None, help="quem pediu (vai para a auditoria)")

    create = commands.add_parser(
        "empresa criar", help="cria a empresa com o administrador e a organização"
    )
    create.add_argument("nome")
    create.add_argument("--organizacao", required=True, help="id ou apelido no Keycloak")
    create.add_argument("--admin", required=True, help="sujeito (sub) de quem administra")
    create.add_argument("--nome-admin", default="")
    create.add_argument("--area", action="append", default=[], help="área; pode repetir")
    create.add_argument("--vagas", type=int, default=1, help="execuções garantidas ao mesmo tempo")
    create.add_argument("--fila", type=int, default=1000, help="máximo de execuções na fila")
    create.add_argument("--emprestimo", type=_factor, default=Decimal("2.0"))
    create.add_argument("--por", default=None, help="quem pediu (vai para a auditoria)")

    usage = commands.add_parser("empresa consumo", help="consumo de um mês, por área e origem")
    usage.add_argument("empresa", type=UUID)
    usage.add_argument("--mes", required=True, help="AAAA-MM (fuso America/Sao_Paulo)")
    usage.add_argument("--json", action="store_true", help="saída em JSON")

    freeze = commands.add_parser("empresa congelar", help="congela a empresa (nada novo começa)")
    freeze.add_argument("empresa", type=UUID)
    freeze.add_argument("--motivo", required=True)
    _owner_options(freeze)

    unfreeze = commands.add_parser("empresa descongelar", help="descongela a empresa")
    unfreeze.add_argument("empresa", type=UUID)
    _owner_options(unfreeze)

    export = commands.add_parser("empresa exportar", help="exporta tudo da empresa (.tar.gz)")
    export.add_argument("empresa", type=UUID)
    export.add_argument("--saida", type=Path, required=True)
    _owner_options(export, folders=True)

    purge = commands.add_parser("empresa apagar", help="apaga uma empresa congelada")
    purge.add_argument("empresa", type=UUID)
    purge.add_argument("--confirmar", required=True, help="o nome exato da empresa")
    purge.add_argument(
        "--segredos-fora-do-openbao",
        action="store_true",
        help=(
            "os segredos das credenciais não estão no OpenBao (o arquivo de desenvolvimento): "
            "apaga sem o cofre configurado (o registro diz quantos ficaram de fora)"
        ),
    )
    _owner_options(purge, folders=True)

    area = commands.add_parser("area", help="cadastra uma área numa empresa")
    area.add_argument("empresa", type=UUID)
    area.add_argument("nome")
    area.add_argument("--por", default=None, help="quem pediu (vai para a auditoria)")

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
    user.add_argument("--por", default=None, help="quem pediu (vai para a auditoria)")
    return parser


def _normalize(argv: Sequence[str]) -> list[str]:
    """``empresa criar ...`` vira o comando ``"empresa criar"`` (``empresa NOME`` não muda)."""
    args = list(argv)
    index = 0
    while index < len(args):
        part = args[index]
        if part == "--url":
            index += 2
            continue
        if part.startswith("-"):
            index += 1
            continue
        if part == "empresa" and index + 1 < len(args) and args[index + 1] in COMPANY_ACTIONS:
            return [*args[:index], f"empresa {args[index + 1]}", *args[index + 2 :]]
        return args
    return args


def _folder(value: Path | None, env: str, default: Path | None = None) -> Path | None:
    if value is not None:
        return value
    raw = os.environ.get(env, "").strip()
    return Path(raw) if raw else default


def _print_usage(report: UsageReport) -> None:
    print(f"Consumo de {report.month} ({report.timezone})")
    for area in report.areas:
        print(
            f"- {area.area_name}: {area.runs} execuções, {area.browser_minutes} min de navegador, "
            f"{area.sessions} sessões ({area.session_minutes} min), {area.batches} lotes "
            f"({area.batch_rows} linhas)"
        )
        for trigger in area.by_trigger:
            states = ", ".join(f"{state.value}: {n}" for state, n in trigger.runs_by_state.items())
            print(
                f"    {trigger.trigger.value}: {trigger.runs} ({states}); {trigger.browser_minutes} min"
            )
    print(
        f"Total: {report.runs} execuções, {report.browser_minutes} min de navegador, "
        f"{report.sessions} sessões ({report.session_minutes} min), {report.batches} lotes "
        f"({report.batch_rows} linhas)"
    )


async def _app_command(args: argparse.Namespace, url: str) -> Any:
    db = Database.from_url(url)
    try:
        if args.command == "empresa criar":
            return await create_company(
                db,
                name=args.nome,
                organization=args.organizacao,
                admin_subject=args.admin,
                admin_name=args.nome_admin,
                area_names=args.area,
                guaranteed_slots=args.vagas,
                max_queued=args.fila,
                max_borrow_factor=args.emprestimo,
                requested_by=args.por or _default_user(),
            )
        if args.command == "empresa consumo":
            month_period(args.mes)
            async with db.tenant_session(args.empresa) as session:
                report = await company_usage(session, args.empresa, args.mes)
            return report
        provisioning = PgProvisioning(db, requested_by=args.por or _default_user())
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


async def _owner_command(args: argparse.Namespace, url: str) -> str:
    engine = create_engine(url)
    action = args.command.removeprefix("empresa ")
    who = args.por or _default_user()
    try:
        if action == "congelar":
            frozen = await freeze_company(
                engine, args.empresa, reason=args.motivo, requested_by=who
            )
            if not frozen.changed:
                return "a empresa já estava congelada"
            return f"empresa congelada; {len(frozen.cancelled)} execuções da fila canceladas"
        if action == "descongelar":
            changed = await unfreeze_company(engine, args.empresa, requested_by=who)
            return "empresa descongelada" if changed else "a empresa já estava ativa"
        files = _folder(args.pasta_arquivos, "RAPA_PASTA_ARQUIVOS")
        artifacts = _folder(args.pasta_artefatos, "RAPA_PASTA_ARTEFATOS") or (
            Path(tempfile.gettempdir()) / "rapa-artefatos"
        )
        # O armazenamento, as chaves e o cofre configurados (RAPA_ARMAZENAMENTO, RAPA_CHAVES,
        # RAPA_COFRE): no S3, a exportação decifra os artefatos e a exclusão apaga os objetos,
        # os segredos do KV e a chave transit da empresa.
        try:
            settings = BackendSettings.from_env()
        except ValueError as exc:
            raise InvalidInput("configuração do armazenamento inválida", [str(exc)]) from None
        backends = Backends(settings)
        try:
            store = backends.artifact_store(artifacts, Database(engine))
            if action == "exportar":
                exported = await export_company(
                    engine, args.empresa, args.saida, files_dir=files, artifacts=store
                )
                return (
                    f"{exported.path}: {sum(exported.tables.values())} linhas em "
                    f"{len(exported.tables)} tabelas e {exported.files} arquivos"
                )
            purged = await purge_company(
                engine,
                args.empresa,
                confirm_name=args.confirmar,
                requested_by=who,
                files_dir=files,
                artifacts=store,
                key_destroyer=backends.key_destroyer(store),
                # Falha fechada: sem o cofre configurado, uma empresa com credenciais só sai
                # se quem pede disser que os segredos não estão no OpenBao (ou com o cofre
                # de desenvolvimento, RAPA_COFRE=dev).
                secrets_outside_vault=args.segredos_fora_do_openbao
                or settings.vault is VaultKind.DEV,
            )
            removed = ", ".join(str(folder) for folder in purged.folders) or "nenhuma"
            if purged.resumed:
                return (
                    "a empresa já tinha saído do banco; "
                    f"{purged.artifacts} artefatos e pastas apagadas: {removed}"
                )
            return (
                f"empresa apagada: {sum(purged.tables.values())} linhas, "
                f"{purged.artifacts} artefatos; pastas apagadas: {removed}"
            )
        finally:
            await backends.aclose()
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(_normalize(sys.argv[1:] if argv is None else argv))
    action = args.command.removeprefix("empresa ")
    try:
        if action in _OWNER_ACTIONS:
            url = args.url_dono or os.environ.get("RAPA_OWNER_DATABASE_URL")
            if not url:
                parser.error("informe --url-dono ou defina RAPA_OWNER_DATABASE_URL")
            print(asyncio.run(_owner_command(args, url)))
            return 0
        url = args.url or os.environ.get("RAPA_DATABASE_URL")
        if not url:
            parser.error("informe --url ou defina RAPA_DATABASE_URL")
        created = asyncio.run(_app_command(args, url))
    except InvalidInput as exc:
        details = f": {'; '.join(exc.errors)}" if exc.errors else ""
        print(f"rapa.control.admin: {exc.message}{details}", file=sys.stderr)
        return 1
    except ContractError as exc:
        print(f"rapa.control.admin: {exc.message}", file=sys.stderr)
        return 1
    except IntegrityError:
        print(
            "rapa.control.admin: já existe ou não confere (empresa, área ou usuário)",
            file=sys.stderr,
        )
        return 1
    except DBAPIError as exc:
        print(f"rapa.control.admin: erro do banco ({type(exc.orig).__name__})", file=sys.stderr)
        return 1
    except FileExistsError:
        print("rapa.control.admin: o arquivo de saída já existe", file=sys.stderr)
        return 1
    if isinstance(created, UsageReport):
        if args.json:
            print(json.dumps(created.model_dump(mode="json"), ensure_ascii=False, indent=2))
        else:
            _print_usage(created)
        return 0
    print(created)
    return 0


if __name__ == "__main__":
    sys.exit(main())
