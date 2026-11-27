"""Entrada e saída de empresas: as operações da plataforma (``python -m rapa.control.admin``).

- **Entrada** (:func:`create_company`, papel da aplicação): a empresa, a cota, as áreas,
  o administrador (papel ``company_admin``) e o vínculo com a organização do provedor de
  identidade (``companies.idp_organization``), numa transação só, com auditoria
  (``empresa.criada``, ``area.criada``, ``usuario.criado``, ``usuario.papeis_alterados``).
  O vínculo é o id ou o apelido, e o formato diz qual: um valor no formato de UUID é o
  id da organização (guardado na forma canônica, a do token); um apelido nunca tem esse
  formato (veja :mod:`rapa.control.auth`).
- **Saída** (dono das tabelas: a aplicação não muda a situação de uma empresa nem apaga
  o que só aceita inclusões):

  - :func:`freeze_company` congela: as execuções ainda na fila (nunca começaram) viram
    ``cancelled`` com o código ``empresa_congelada`` e auditoria; as que estão rodando
    terminam normalmente (matar no meio deixaria o resultado desconhecido). Daí em
    diante, nada novo começa (``ensure_company_active``) e o despachante não pega nada
    dela. :func:`unfreeze_company` volta (o que foi cancelado continua cancelado).
  - :func:`export_company` grava um ``.tar.gz`` (permissão 0600) com um JSONL por tabela
    da empresa (toda tabela com ``company_id``, mais a linha de ``companies``, em ordem
    estável: das tabelas-mãe para as filhas e, dentro de cada uma, pela chave primária),
    um ``manifesto.json`` com o SHA-256 e o número de linhas de cada arquivo, os arquivos
    de entrada da empresa (``RAPA_PASTA_ARQUIVOS/<empresa>``, sem seguir links: cada um é
    aberto uma vez, com ``O_NOFOLLOW``) e os artefatos dela, pelo
    :class:`rapa.control.artifacts.ArtifactStore` (``list_company`` e ``open``: no store
    de objetos, decifrados, como a pessoa os vê pela API). Tudo é copiado para a pasta de
    trabalho, de onde sai o ``.tar.gz``, então o SHA-256 do manifesto é sempre o dos
    bytes guardados. Nunca exporta material de chave: o sal dos webhooks e as chaves
    embrulhadas das execuções (e qualquer coluna ``bytea``) saem como ``null``. Exporta
    empresa ativa ou congelada, numa leitura consistente (``REPEATABLE READ``, só
    leitura).
  - :func:`purge_company` apaga uma empresa congelada (o nome confirmado), sem execuções
    rodando nem sessões abertas (dentro do prazo: uma sessão que ficou aberta no banco
    depois de ``expires_at`` não tem mais navegador), numa transação só que marca
    ``rapa.purge_company`` (as
    travas de "só inclusões" aceitam a exclusão só assim, e só do dono): apaga todas as
    linhas da empresa em todas as tabelas, das filhas para as mães, e por fim a linha de
    ``companies``; ainda dentro da transação, apaga os artefatos da empresa pelo store
    (``delete_company``) e chama o :class:`KeyDestroyer` (no OpenBao: os segredos do KV,
    a chave transit e os objetos da empresa); e por fim grava ``empresa.apagada`` em
    ``platform_events`` (o id, o SHA-256 do nome, as contagens por tabela, quantos
    artefatos e o que o destruidor de chaves apagou, só contagens, quem pediu e quando).
    Se o store ou o cofre falharem, nada do banco é apagado e o comando pode ser
    repetido (o que já saiu do cofre não volta, e a repetição conta zero). Depois de
    confirmada a transação, apaga a pasta dos arquivos de entrada da empresa. Se
    parar depois das linhas (essa pasta que não sai), rodar de novo com o mesmo nome
    confere o ``empresa.apagada`` (pelo SHA-256 do nome) e termina as pastas, os
    artefatos e as chaves (o store e o destruidor de novo: o que já saiu conta zero).

    Falha fechada na configuração: antes de apagar qualquer linha, confere que o que a
    empresa tem fora do banco vai junto. Com artefatos no store de objetos (linhas em
    ``run_keys``), o store precisa ser o de objetos; com chaves embrulhadas pelo transit
    do OpenBao, o destruidor precisa apagar a chave transit; com credenciais
    registradas, o destruidor precisa apagar os segredos do KV, a não ser que quem pede
    diga que eles não estão no OpenBao (``secrets_outside_vault``: o arquivo de
    desenvolvimento), e então ``empresa.apagada`` diz quantas credenciais ficaram com o
    segredo fora (``segredos_fora_do_openbao``). Senão, :class:`InvalidInput` com o que
    falta configurar, e nada sai.

Congelar e apagar tomam primeiro a trava da situação da empresa, exclusiva: esperam as
escritas em andamento terminarem, e as que chegam depois já veem a empresa congelada (ou
apagada).
"""

import contextlib
import hashlib
import io
import json
import logging
import os
import shutil
import stat
import tarfile
import tempfile
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from rapa.control.artifacts import (
    ArtifactCorrupted,
    ArtifactMissing,
    ArtifactStore,
    LocalArtifactStore,
)
from rapa.control.db.schema import (
    areas,
    companies,
    company_quotas,
    memberships,
    platform_events,
    runs,
    users,
)
from rapa.control.db.session import Database, tenant_session
from rapa.control.services import audit
from rapa.control.services._common import (
    COMPANY_STATUS_LOCK_CLASS,
    PLATFORM_ACTOR,
    now_utc,
    platform_actor,
)
from rapa.control.services.company import clean_area_name
from rapa.control.services.queue import COMPANY_FROZEN_CODE, COMPANY_FROZEN_RUN_MESSAGE
from rapa.core.contracts import (
    CompanyStatus,
    Conflict,
    Forbidden,
    InvalidInput,
    NotFound,
    Role,
    RunState,
)
from rapa.core.ids import new_id

__all__ = [
    "EXPORT_FORMAT",
    "PLATFORM_ACTOR",
    "CompanyTable",
    "ExportReport",
    "FreezeReport",
    "KeyDestroyer",
    "LocalKeyDestroyer",
    "PurgeReport",
    "create_company",
    "export_company",
    "freeze_company",
    "purge_company",
    "tenant_tables",
    "unfreeze_company",
]

logger = logging.getLogger("rapa.control.lifecycle")

#: Versão do formato da exportação (no ``manifesto.json``).
EXPORT_FORMAT = 1
_MAX_REASON = 500
_MAX_ORGANIZATION = 255
_MAX_NAME = 200
#: Colunas que são material de chave e saem como ``null`` (além de toda coluna ``bytea``).
_KEY_COLUMNS: dict[str, frozenset[str]] = {"webhooks": frozenset({"key_salt"})}
_SET_TENANT = text("SELECT set_config('rapa.company_id', :company, true)")
_NOT_OWNER = (
    "esta operação é da plataforma: use a URL do dono das tabelas "
    "(--url-dono ou RAPA_OWNER_DATABASE_URL)"
)


_actor = platform_actor


def _clean(value: str, what: str, limit: int) -> str:
    cleaned = " ".join(value.split())
    if not cleaned or len(cleaned) > limit or any(ord(c) < 32 or ord(c) == 127 for c in cleaned):
        raise InvalidInput(f"{what} inválido", [f"use de 1 a {limit} caracteres visíveis"])
    return cleaned


# ---------------------------------------------------------------------------
# Entrada
# ---------------------------------------------------------------------------


async def create_company(
    db: Database,
    *,
    name: str,
    organization: str,
    admin_subject: str,
    admin_name: str = "",
    area_names: Sequence[str] = (),
    guaranteed_slots: int = 1,
    max_queued: int = 1000,
    max_borrow_factor: Decimal | float = Decimal("2.0"),
    requested_by: str = "cli",
) -> UUID:
    """Cria a empresa inteira numa transação só (papel da aplicação) e devolve o id."""
    name = _clean(name, "nome da empresa", _MAX_NAME)
    organization = organization.strip()
    if not 1 <= len(organization) <= _MAX_ORGANIZATION or not organization.isprintable():
        raise InvalidInput(
            "organização inválida",
            [f"use o id ou o apelido da organização (até {_MAX_ORGANIZATION} caracteres)"],
        )
    with contextlib.suppress(ValueError):
        # No formato de UUID, é o id da organização: guardado como o token o traz.
        organization = str(UUID(organization))
    subject = admin_subject.strip()
    if not 1 <= len(subject) <= 255 or not subject.isprintable():
        raise InvalidInput("sujeito do administrador inválido", ["use o sub do token (até 255)"])
    display = " ".join(admin_name.split())[:200]
    cleaned_areas = list(dict.fromkeys(clean_area_name(a) for a in area_names))
    factor = Decimal(str(max_borrow_factor))
    problems = []
    if guaranteed_slots < 0:
        problems.append("--vagas não pode ser negativo")
    if max_queued < 0:
        problems.append("--fila não pode ser negativo")
    if factor < 1 or factor >= 1000:
        problems.append("--emprestimo precisa estar entre 1 e 999")
    if problems:
        raise InvalidInput("cota inválida", problems)
    company_id = new_id()
    actor = _actor(requested_by)
    async with db.tenant_session(company_id) as session:
        linked = await session.scalar(select(func.rapa_company_for_organization(organization)))
        if linked is not None:
            raise Conflict("esta organização já está ligada a outra empresa")
        await session.execute(
            insert(companies).values(id=company_id, name=name, idp_organization=organization)
        )
        await session.execute(
            insert(company_quotas).values(
                company_id=company_id,
                guaranteed_slots=guaranteed_slots,
                max_borrow_factor=factor,
                max_queued=max_queued,
            )
        )
        entries = []
        for area_name in cleaned_areas:
            area_id = new_id()
            await session.execute(
                insert(areas).values(id=area_id, company_id=company_id, name=area_name)
            )
            entries.append({"target": f"area:{area_id}", "payload": {"nome": area_name}})
        user_id = new_id()
        created = await session.scalar(
            pg_insert(users)
            .values(id=user_id, company_id=company_id, subject=subject, display_name=display)
            .on_conflict_do_nothing(index_elements=["subject"])
            .returning(users.c.id)
        )
        if created is None:
            raise Conflict("este sujeito já é usuário de outra empresa")
        await session.execute(
            insert(memberships).values(
                id=new_id(),
                company_id=company_id,
                user_id=user_id,
                area_id=None,
                role=Role.COMPANY_ADMIN.value,
            )
        )
        await audit.append(
            session,
            company_id=company_id,
            actor=actor,
            action="empresa.criada",
            target=f"empresa:{company_id}",
            payload={
                "organizacao": organization,
                "vagas": guaranteed_slots,
                "fila": max_queued,
                "emprestimo": str(factor),
                "areas": len(cleaned_areas),
            },
        )
        await audit.append_many(
            session, company_id=company_id, actor=actor, action="area.criada", entries=entries
        )
        await audit.append(
            session,
            company_id=company_id,
            actor=actor,
            action="usuario.criado",
            target=f"usuario:{user_id}",
            payload={"usuario": str(user_id)},
        )
        await audit.append(
            session,
            company_id=company_id,
            actor=actor,
            action="usuario.papeis_alterados",
            target=f"usuario:{user_id}",
            payload={"adicionados": [f"{Role.COMPANY_ADMIN.value}@empresa"], "removidos": []},
        )
    return company_id


# ---------------------------------------------------------------------------
# Congelar e descongelar
# ---------------------------------------------------------------------------


async def _require_owner(session: AsyncSession) -> None:
    owner = await session.scalar(
        text(
            "SELECT c.relowner = (SELECT r.oid FROM pg_roles r WHERE r.rolname = current_user) "
            "FROM pg_class c WHERE c.oid = 'public.companies'::regclass"
        )
    )
    if not owner:
        raise Forbidden(_NOT_OWNER)


async def _lock_exclusive(session: AsyncSession, company_id: UUID) -> None:
    await session.execute(
        select(
            func.pg_advisory_xact_lock(COMPANY_STATUS_LOCK_CLASS, func.hashtext(str(company_id)))
        )
    )


async def _company_row(session: AsyncSession, company_id: UUID) -> Any:
    row = (
        await session.execute(
            select(companies.c.id, companies.c.name, companies.c.status)
            .where(companies.c.id == company_id)
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise NotFound("empresa não encontrada")
    return row


@dataclass(frozen=True, slots=True)
class FreezeReport:
    """O que congelar fez: as execuções da fila canceladas, ou nada (já estava congelada)."""

    changed: bool
    cancelled: list[UUID] = field(default_factory=list)


async def freeze_company(
    owner_engine: AsyncEngine, company_id: UUID, *, reason: str, requested_by: str = "cli"
) -> FreezeReport:
    """Congela a empresa (dono das tabelas) e cancela a fila dela."""
    reason = _clean(reason, "motivo", _MAX_REASON)
    actor = _actor(requested_by)
    async with tenant_session(owner_engine, company_id) as session:
        await _require_owner(session)
        await _lock_exclusive(session, company_id)
        row = await _company_row(session, company_id)
        if row.status == CompanyStatus.FROZEN.value:
            return FreezeReport(changed=False)
        await session.execute(
            update(companies)
            .where(companies.c.id == company_id)
            .values(
                status=CompanyStatus.FROZEN.value,
                status_reason=reason,
                status_changed_at=func.now(),
            )
        )
        cancelled = list(
            (
                await session.scalars(
                    update(runs)
                    .where(runs.c.company_id == company_id, runs.c.state == RunState.QUEUED.value)
                    .values(
                        state=RunState.CANCELLED.value,
                        finished_at=func.now(),
                        error_code=COMPANY_FROZEN_CODE,
                        error_message=COMPANY_FROZEN_RUN_MESSAGE,
                    )
                    .returning(runs.c.id)
                )
            ).all()
        )
        await audit.append(
            session,
            company_id=company_id,
            actor=actor,
            action="empresa.congelada",
            target=f"empresa:{company_id}",
            payload={"motivo": reason, "canceladas": len(cancelled)},
        )
        await audit.append_many(
            session,
            company_id=company_id,
            actor=actor,
            action="execucao.cancelada",
            entries=[
                {"target": f"execucao:{run_id}", "payload": {"erro": COMPANY_FROZEN_CODE}}
                for run_id in sorted(cancelled)
            ],
        )
    return FreezeReport(changed=True, cancelled=cancelled)


async def unfreeze_company(
    owner_engine: AsyncEngine, company_id: UUID, *, requested_by: str = "cli"
) -> bool:
    """Descongela a empresa (dono das tabelas); ``False`` se ela já estava ativa."""
    actor = _actor(requested_by)
    async with tenant_session(owner_engine, company_id) as session:
        await _require_owner(session)
        await _lock_exclusive(session, company_id)
        row = await _company_row(session, company_id)
        if row.status == CompanyStatus.ACTIVE.value:
            return False
        await session.execute(
            update(companies)
            .where(companies.c.id == company_id)
            .values(
                status=CompanyStatus.ACTIVE.value,
                status_reason=None,
                status_changed_at=func.now(),
            )
        )
        await audit.append(
            session,
            company_id=company_id,
            actor=actor,
            action="empresa.descongelada",
            target=f"empresa:{company_id}",
            payload={"empresa": str(company_id)},
        )
    return True


# ---------------------------------------------------------------------------
# As tabelas da empresa, pelo catálogo
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CompanyTable:
    """Uma tabela com ``company_id``, como o catálogo a descreve."""

    name: str
    #: Colunas na ordem da tabela, sem as geradas (que o banco calcula).
    columns: tuple[str, ...]
    #: Colunas que saem como ``null`` na exportação (material de chave).
    redacted: tuple[str, ...]
    generated: tuple[str, ...]
    primary_key: tuple[str, ...]


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


async def tenant_tables(session: AsyncSession) -> list[CompanyTable]:
    """Toda tabela do esquema com ``company_id`` (sem as partições), das mães para as filhas.

    A ordem segue as chaves estrangeiras (e o nome, no empate): apagar na ordem inversa
    nunca esbarra numa chave. Tabelas novas entram sozinhas, sem lista para esquecer.
    """
    names = set(
        (
            await session.scalars(
                text(
                    "SELECT c.relname FROM pg_class c "
                    "WHERE c.relnamespace = 'public'::regnamespace "
                    "AND c.relkind IN ('r', 'p') AND NOT c.relispartition "
                    "AND EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid "
                    "AND a.attname = 'company_id' AND NOT a.attisdropped)"
                )
            )
        ).all()
    )
    edges = (
        await session.execute(
            text(
                "SELECT ch.relname, pa.relname FROM pg_constraint con "
                "JOIN pg_class ch ON ch.oid = con.conrelid "
                "JOIN pg_class pa ON pa.oid = con.confrelid "
                "WHERE con.contype = 'f' AND ch.relnamespace = 'public'::regnamespace"
            )
        )
    ).all()
    parents: dict[str, set[str]] = {name: set() for name in names}
    for child, parent in edges:
        if child in names and parent in names and child != parent:
            parents[child].add(parent)
    order: list[str] = []
    ready = sorted(name for name, deps in parents.items() if not deps)
    while ready:
        current = ready.pop(0)
        order.append(current)
        for name in sorted(names):
            deps = parents[name]
            if current in deps:
                deps.discard(current)
                if not deps:
                    ready.append(name)
        ready.sort()
    if len(order) != len(names):  # pragma: no cover - o esquema não tem ciclos
        raise RuntimeError("as chaves estrangeiras das tabelas da empresa formam um ciclo")
    tables = []
    for name in order:
        columns = (
            await session.execute(
                text(
                    "SELECT a.attname, t.typname, a.attgenerated <> '' FROM pg_attribute a "
                    "JOIN pg_type t ON t.oid = a.atttypid "
                    "WHERE a.attrelid = to_regclass('public.' || quote_ident(:name)) "
                    "AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum"
                ),
                {"name": name},
            )
        ).all()
        key = (
            await session.scalars(
                text(
                    "SELECT a.attname FROM pg_index i "
                    "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
                    "WHERE i.indrelid = to_regclass('public.' || quote_ident(:name)) "
                    "AND i.indisprimary ORDER BY array_position(i.indkey::int2[], a.attnum)"
                ),
                {"name": name},
            )
        ).all()
        generated = tuple(col for col, _, gen in columns if gen)
        redacted = tuple(
            col
            for col, typname, gen in columns
            if not gen and (typname == "bytea" or col in _KEY_COLUMNS.get(name, frozenset()))
        )
        tables.append(
            CompanyTable(
                name=name,
                columns=tuple(col for col, _, gen in columns if not gen),
                redacted=redacted,
                generated=generated,
                primary_key=tuple(key),
            )
        )
    return tables


# ---------------------------------------------------------------------------
# Exportar
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExportReport:
    path: Path
    #: Linhas exportadas por tabela.
    tables: dict[str, int]
    files: int


@asynccontextmanager
async def _snapshot(engine: AsyncEngine, company_id: UUID) -> AsyncIterator[AsyncSession]:
    """Uma leitura consistente (``REPEATABLE READ``, só leitura) fixada na empresa."""
    consistent = engine.execution_options(
        isolation_level="REPEATABLE READ", postgresql_readonly=True
    )
    async with AsyncSession(consistent, expire_on_commit=False, autoflush=False) as session:
        async with session.begin():
            await session.execute(_SET_TENANT, {"company": str(company_id)})
            await session.execute(text("SET LOCAL TimeZone = 'UTC'"))
            yield session


def _row_expression(table: CompanyTable) -> str:
    expression = "to_jsonb(t)"
    if table.generated:
        dropped = ", ".join(_literal(c) for c in table.generated)
        expression = f"({expression} - ARRAY[{dropped}]::text[])"
    if table.redacted:
        nulls = ", ".join(f"{_literal(c)}, NULL" for c in table.redacted)
        expression = f"({expression} || jsonb_build_object({nulls}))"
    return f"({expression})::text"


async def _write_table(
    session: AsyncSession, company_id: UUID, table: CompanyTable, path: Path
) -> tuple[int, str]:
    order = ", ".join(f"t.{_quote(c)}" for c in table.primary_key) or "1"
    column = "id" if table.name == "companies" else "company_id"
    query = text(
        f"SELECT {_row_expression(table)} FROM public.{_quote(table.name)} t "  # noqa: S608 - nomes do catálogo
        f"WHERE t.{column} = :company ORDER BY {order}"
    )
    digest = hashlib.sha256()
    lines = 0
    with path.open("wb") as out:
        result = await session.stream(query, {"company": company_id})
        async for (row,) in result:
            data = (row + "\n").encode()
            out.write(data)
            digest.update(data)
            lines += 1
    return lines, digest.hexdigest()


@dataclass(frozen=True, slots=True)
class _CopiedFile:
    """Um arquivo da empresa, copiado para a pasta de trabalho da exportação."""

    #: O nome no ``.tar.gz`` (``arquivos/...`` ou ``artefatos/...``).
    name: str
    #: A cópia (o que vai para o ``.tar.gz``: os mesmos bytes do SHA-256 do manifesto).
    copy: Path
    size: int
    sha256: str


_OPEN_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)


def _copy_open_file(fd: int, target: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with os.fdopen(fd, "rb", closefd=False) as source, target.open("xb") as out:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
            out.write(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def _company_files(
    base: Path | None, company_id: UUID, prefix: str, work: Path
) -> list[_CopiedFile]:
    """Copia os arquivos comuns da pasta da empresa para ``work``, sem seguir links.

    A pasta é percorrida por descritores (``os.fwalk``, sem seguir links: uma subpasta
    trocada por um link no meio do caminho é deixada de fora) e cada arquivo é aberto
    uma vez só, relativo à pasta dele e com ``O_NOFOLLOW``: um arquivo trocado por um
    link depois da listagem não é seguido. O que não é arquivo comum (link, fila,
    dispositivo) fica de fora. O SHA-256 é calculado sobre os bytes copiados, e é a
    cópia que entra no ``.tar.gz``: o manifesto sempre confere com o arquivo.
    """
    if base is None:
        return []
    root = base / str(company_id)
    try:
        info = os.lstat(root)
    except FileNotFoundError:
        return []
    if not stat.S_ISDIR(info.st_mode):
        logger.warning("a pasta da empresa não é uma pasta comum; fica de fora da exportação")
        return []
    target_dir = work / prefix
    target_dir.mkdir(mode=0o700)
    found: list[_CopiedFile] = []
    for folder, dirs, files, dir_fd in os.fwalk(root, follow_symlinks=False):
        dirs.sort()
        for name in sorted(files):
            try:
                fd = os.open(name, _OPEN_FILE, dir_fd=dir_fd)
            except OSError:
                continue  # um link (ELOOP), um soquete ou algo que sumiu: fica de fora
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    continue
                copy = target_dir / f"{len(found):08d}"
                size, digest = _copy_open_file(fd, copy)
            finally:
                os.close(fd)
            relative = Path(folder, name).relative_to(root).as_posix()
            found.append(_CopiedFile(f"{prefix}/{relative}", copy, size, digest))
    return found


def _store_for(artifacts: ArtifactStore | None, artifacts_dir: Path | None) -> ArtifactStore | None:
    """O store dos artefatos: o dado, ou o local de ``artifacts_dir`` (a disposição de sempre)."""
    if artifacts is not None:
        return artifacts
    return LocalArtifactStore(artifacts_dir) if artifacts_dir is not None else None


async def _store_files(
    store: ArtifactStore | None, company_id: UUID, work: Path
) -> tuple[list[_CopiedFile], int]:
    """Copia os artefatos da empresa, como o store os entrega (decifrados), para ``work``.

    Devolve as cópias e quantos não puderam ser lidos (sumiram entre a listagem e a
    leitura, perderam a chave, ou não conferem, já na abertura ou no meio: ficam de
    fora, com um aviso no log e a contagem no manifesto).
    """
    if store is None:
        return [], 0
    target_dir = work / "artefatos"
    target_dir.mkdir(mode=0o700)
    found: list[_CopiedFile] = []
    unreadable = 0
    async for item in store.list_company(company_id):
        try:
            reader = await store.open(company_id, item.run_id, item.attempt, item.name)
        except ArtifactMissing, ArtifactCorrupted:
            # Sumiu, ficou sem a chave, ou o objeto está danificado (o cabeçalho, o
            # tamanho): fica de fora, contado. O cofre ou o S3 fora do ar
            # (ArtifactUnavailable) param a exportação: o comando é repetido.
            unreadable += 1
            continue
        copy = target_dir / f"{len(found):08d}"
        digest = hashlib.sha256()
        size = 0
        try:
            with copy.open("xb") as out:
                async for chunk in reader.chunks():
                    digest.update(chunk)
                    out.write(chunk)
                    size += len(chunk)
        except ArtifactCorrupted:
            copy.unlink(missing_ok=True)
            unreadable += 1
            continue
        finally:
            await reader.aclose()
        name = f"artefatos/{item.run_id}/{item.attempt}/{item.name}"
        found.append(_CopiedFile(name, copy, size, digest.hexdigest()))
    if unreadable:
        logger.warning(
            "exportação: %d artefato(s) da empresa não puderam ser lidos e ficaram de fora",
            unreadable,
        )
    return found, unreadable


def _member(name: str, size: int, mtime: datetime) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mode = 0o600
    info.mtime = int(mtime.timestamp())
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


@contextlib.contextmanager
def _exclusive_output(path: Path) -> Iterator[io.BufferedWriter]:
    """Cria o arquivo de saída com permissão 0600; nunca sobrescreve um que já existe."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    handle = os.fdopen(fd, "wb")
    try:
        yield handle
    except BaseException:
        handle.close()
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        raise
    else:
        handle.close()


def _check_output(output: Path) -> None:
    if output.exists() or output.is_symlink():
        raise Conflict("o arquivo de saída já existe; escolha outro nome")
    if not output.parent.is_dir():
        raise InvalidInput("pasta de saída inválida", ["a pasta do arquivo de saída não existe"])


async def export_company(
    owner_engine: AsyncEngine,
    company_id: UUID,
    output: Path,
    *,
    files_dir: Path | None = None,
    artifacts_dir: Path | None = None,
    artifacts: ArtifactStore | None = None,
) -> ExportReport:
    """Exporta tudo da empresa para ``output`` (``.tar.gz``, 0600; recusa se já existe).

    Os artefatos vêm de ``artifacts`` (o store configurado) ou, sem ele, do store local
    de ``artifacts_dir``.
    """
    _check_output(output)
    exported_at = now_utc()
    work = Path(tempfile.mkdtemp(prefix="rapa-exportacao-"))
    try:
        entries: list[dict[str, Any]] = []
        async with _snapshot(owner_engine, company_id) as session:
            await _require_owner(session)
            company = (
                await session.execute(
                    select(companies.c.id, companies.c.name, companies.c.status).where(
                        companies.c.id == company_id
                    )
                )
            ).one_or_none()
            if company is None:
                raise NotFound("empresa não encontrada")
            companies_table = CompanyTable(
                name="companies",
                columns=tuple(c.name for c in companies.columns),
                redacted=(),
                generated=(),
                primary_key=("id",),
            )
            for table in [companies_table, *await tenant_tables(session)]:
                file_name = f"tabelas/{table.name}.jsonl"
                lines, digest = await _write_table(
                    session, company_id, table, work / f"{table.name}.jsonl"
                )
                entries.append(
                    {
                        "tabela": table.name,
                        "arquivo": file_name,
                        "linhas": lines,
                        "sha256": digest,
                        "colunas_sem_valor": list(table.redacted),
                    }
                )
        stored, unreadable = await _store_files(
            _store_for(artifacts, artifacts_dir), company_id, work
        )
        files = [*_company_files(files_dir, company_id, "arquivos", work), *stored]
        file_entries: list[dict[str, Any]] = [
            {"arquivo": copied.name, "bytes": copied.size, "sha256": copied.sha256}
            for copied in files
        ]
        manifest = {
            "formato": EXPORT_FORMAT,
            "empresa": {"id": str(company[0]), "nome": company[1], "situacao": company[2]},
            "gerado_em": exported_at.isoformat(),
            "tabelas": entries,
            "arquivos": file_entries,
            "artefatos_ilegiveis": unreadable,
            "sem_valor": (
                "material de chave (o sal dos webhooks, as chaves embrulhadas das execuções e "
                "colunas binárias) sai como null"
            ),
        }
        manifest_bytes = (
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode()
        with _exclusive_output(output) as handle, tarfile.open(fileobj=handle, mode="w:gz") as tar:
            tar.addfile(
                _member("manifesto.json", len(manifest_bytes), exported_at),
                io.BytesIO(manifest_bytes),
            )
            for entry in entries:
                source = work / f"{entry['tabela']}.jsonl"
                with source.open("rb") as data:
                    tar.addfile(_member(entry["arquivo"], source.stat().st_size, exported_at), data)
            for copied in files:
                with copied.copy.open("rb") as data:
                    tar.addfile(_member(copied.name, copied.size, exported_at), data)
        return ExportReport(
            path=output,
            tables={entry["tabela"]: entry["linhas"] for entry in entries},
            files=len(file_entries),
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------
# Apagar
# ---------------------------------------------------------------------------


class KeyDestroyer(Protocol):
    """Destrói as chaves próprias de uma empresa apagada (dentro de :func:`purge_company`).

    Devolve o que apagou, só como contagens (vão para o registro ``empresa.apagada``),
    ou ``None``. :class:`rapa.control.vault.OpenBaoKeyDestroyer` apaga os segredos do KV,
    a chave transit e os objetos da empresa. Os sais dos webhooks e as chaves embrulhadas
    das execuções somem com as linhas, então as chaves derivadas deles não voltam.

    ``destroys_secrets`` e ``destroys_transit_keys`` (opcionais; sem eles, falso) dizem o
    que ele alcança: :func:`purge_company` recusa apagar uma empresa com credenciais ou
    chaves do transit que o destruidor não alcança.
    Pode ser chamado de novo para a mesma empresa (a volta que termina uma exclusão
    interrompida).
    """

    async def destroy(self, company_id: UUID) -> Mapping[str, int] | None: ...


class LocalKeyDestroyer:
    """Sem cofre: a empresa não tem chave própria; só registra."""

    destroys_secrets = False
    destroys_transit_keys = False

    async def destroy(self, company_id: UUID) -> Mapping[str, int] | None:
        logger.info(
            "empresa %s apagada: nenhuma chave própria para destruir sem o cofre", company_id
        )
        return {}


@dataclass(frozen=True, slots=True)
class PurgeReport:
    #: Linhas apagadas por tabela (inclusive ``companies``).
    tables: dict[str, int]
    #: Pastas da empresa apagadas (a dos arquivos de entrada).
    folders: list[Path] = field(default_factory=list)
    #: As linhas já tinham saído numa volta anterior; esta só terminou pastas e chaves.
    resumed: bool = False
    #: Artefatos (arquivos ou objetos) apagados pelo store.
    artifacts: int = 0
    #: O que o destruidor de chaves apagou (contagens).
    keys: dict[str, int] = field(default_factory=dict)


def _remove_company_folder(base: Path | None, company_id: UUID) -> Path | None:
    if base is None:
        return None
    root = base / str(company_id)
    try:
        info = os.lstat(root)
    except FileNotFoundError:
        return None
    if stat.S_ISDIR(info.st_mode):
        shutil.rmtree(root)
    else:
        root.unlink()  # um link ou arquivo no lugar da pasta: some só ele, nunca o destino
    return root


async def _purged_before(owner_engine: AsyncEngine, company_id: UUID, confirm_name: str) -> bool:
    """A empresa já saiu do banco com este nome (o registro ``empresa.apagada``)."""
    async with tenant_session(owner_engine, company_id) as session:
        await _require_owner(session)
        await _lock_exclusive(session, company_id)
        if await session.scalar(select(companies.c.id).where(companies.c.id == company_id)):
            return False
        recorded = await session.scalar(
            select(func.count())
            .select_from(platform_events)
            .where(
                platform_events.c.action == "empresa.apagada",
                platform_events.c.target == f"empresa:{company_id}",
                platform_events.c.data["nome_sha256"].astext
                == hashlib.sha256(confirm_name.encode()).hexdigest(),
            )
        )
        return bool(recorded)


async def _finish_purge(
    company_id: UUID,
    files_dir: Path | None,
    store: ArtifactStore | None,
    destroyer: KeyDestroyer,
) -> PurgeReport:
    """A volta que termina uma exclusão cujas linhas já saíram (o mesmo nome confirmado).

    O store e o destruidor rodam de novo (o que já saiu conta zero) e a pasta dos
    arquivos de entrada sai.
    """
    removed = await store.delete_company(company_id) if store is not None else 0
    destroyed = {
        str(what): int(count) for what, count in (await destroyer.destroy(company_id) or {}).items()
    }
    folder = _remove_company_folder(files_dir, company_id)
    return PurgeReport(
        tables={},
        folders=[folder] if folder is not None else [],
        artifacts=removed,
        keys=destroyed,
        resumed=True,
    )


async def _outside_problems(
    session: AsyncSession,
    company_id: UUID,
    store: ArtifactStore | None,
    destroyer: KeyDestroyer,
    *,
    secrets_outside_vault: bool,
) -> tuple[list[str], int]:
    """O que a empresa tem fora do banco e a configuração não alcança (nada, se tudo vai).

    Devolve os problemas e quantas credenciais ficam com o segredo fora do OpenBao (o
    pedido disse que não estão lá).
    """
    keys = (
        await session.execute(
            text(
                "SELECT count(*) AS total, count(*) FILTER "
                "(WHERE substring(wrapped_key FROM 1 FOR 6) = 'vault:'::bytea) AS transit "
                "FROM run_keys WHERE company_id = :company"
            ),
            {"company": company_id},
        )
    ).one()
    registered = int(
        await session.scalar(
            text("SELECT count(*) FROM credentials WHERE company_id = :company"),
            {"company": company_id},
        )
        or 0
    )
    problems: list[str] = []
    if keys.total and (store is None or isinstance(store, LocalArtifactStore)):
        problems.append(
            f"a empresa tem artefatos cifrados no S3 ({keys.total} execução(ões) com chave), "
            "mas o armazenamento configurado é o local: configure RAPA_ARMAZENAMENTO=s3 e "
            "RAPA_S3_* (os mesmos do servidor)"
        )
    if keys.transit and not getattr(destroyer, "destroys_transit_keys", False):
        problems.append(
            "as chaves das execuções da empresa são do transit do OpenBao, mas a configuração "
            "não alcança a chave dela: configure RAPA_CHAVES=openbao e RAPA_BAO_* (papel "
            "rapa-operacao)"
        )
    outside = 0
    if registered and not getattr(destroyer, "destroys_secrets", False):
        if secrets_outside_vault:
            outside = registered
        else:
            problems.append(
                f"a empresa tem {registered} credencial(is) registrada(s), mas o cofre não está "
                "configurado: os segredos ficariam no OpenBao. Configure RAPA_COFRE=openbao e "
                "RAPA_BAO_* (papel rapa-operacao) ou, se os segredos não estão no OpenBao "
                "(o arquivo de desenvolvimento), diga com --segredos-fora-do-openbao"
            )
    return problems, outside


async def purge_company(
    owner_engine: AsyncEngine,
    company_id: UUID,
    *,
    confirm_name: str,
    requested_by: str = "cli",
    files_dir: Path | None = None,
    artifacts_dir: Path | None = None,
    artifacts: ArtifactStore | None = None,
    key_destroyer: KeyDestroyer | None = None,
    secrets_outside_vault: bool = False,
) -> PurgeReport:
    """Apaga a empresa congelada inteira (dono das tabelas); veja o início do módulo.

    Os artefatos saem por ``artifacts`` (o store configurado) ou, sem ele, pelo store
    local de ``artifacts_dir``. Com ``secrets_outside_vault``, quem pede diz que os
    segredos das credenciais não estão no OpenBao (o arquivo de desenvolvimento): a
    empresa sai mesmo sem um destruidor que apague segredos.
    """
    actor = _actor(requested_by)
    store = _store_for(artifacts, artifacts_dir)
    destroyer: KeyDestroyer = key_destroyer or LocalKeyDestroyer()
    # As linhas saem antes da pasta dos arquivos de entrada (o store e as chaves saem
    # dentro da transação): uma volta que parou depois delas (a pasta que não saiu)
    # termina quando o comando roda de novo com o mesmo nome.
    if await _purged_before(owner_engine, company_id, confirm_name):
        return await _finish_purge(company_id, files_dir, store, destroyer)
    async with tenant_session(owner_engine, company_id) as session:
        await _require_owner(session)
        await _lock_exclusive(session, company_id)
        row = await _company_row(session, company_id)
        if confirm_name != row.name:
            raise InvalidInput(
                "o nome não confere", ["--confirmar precisa ser o nome exato da empresa"]
            )
        if row.status != CompanyStatus.FROZEN.value:
            raise Conflict("só uma empresa congelada pode ser apagada; congele antes")
        running = await session.scalar(
            text(
                "SELECT count(*) FROM runs WHERE company_id = :company "
                "AND state IN ('running', 'awaiting_human')"
            ),
            {"company": company_id},
        )
        if running:
            raise Conflict("a empresa ainda tem execuções rodando; espere elas terminarem")
        # Só as que ainda estão no prazo: uma sessão que ficou aberta no banco depois dele
        # (a réplica da web que a segurava caiu, e nenhuma outra a encerrou) não tem mais
        # navegador (o pod sai no prazo máximo) e não conta em limite nenhum.
        live = await session.scalar(
            text(
                "SELECT count(*) FROM recording_sessions WHERE company_id = :company "
                "AND status IN ('starting', 'active') AND expires_at > now()"
            ),
            {"company": company_id},
        )
        if live:
            raise Conflict("a empresa ainda tem sessões de gravação abertas")
        problems, secrets_left = await _outside_problems(
            session, company_id, store, destroyer, secrets_outside_vault=secrets_outside_vault
        )
        if problems:
            raise InvalidInput(
                "a configuração não alcança tudo o que a empresa tem fora do banco; "
                "nada foi apagado",
                problems,
            )
        tables = await tenant_tables(session)
        counts: dict[str, int] = {}
        for table in tables:
            counts[table.name] = int(
                await session.scalar(
                    text(
                        f"SELECT count(*) FROM public.{_quote(table.name)} "  # noqa: S608
                        "WHERE company_id = :company"
                    ),
                    {"company": company_id},
                )
                or 0
            )
        counts["companies"] = 1
        # Só nesta transação, do dono, as travas de "só inclusões" aceitam apagar.
        await session.execute(
            text("SELECT set_config('rapa.purge_company', :company, true)"),
            {"company": str(company_id)},
        )
        for table in reversed(tables):
            deleted = await session.execute(
                text(f"DELETE FROM public.{_quote(table.name)} WHERE company_id = :company"),  # noqa: S608
                {"company": company_id},
            )
            if deleted.rowcount != counts[table.name]:  # type: ignore[attr-defined]
                raise Conflict(
                    f"a tabela {table.name} mudou durante a exclusão; nada foi apagado, tente de novo"
                )
        await session.execute(
            text("DELETE FROM public.companies WHERE id = :company"), {"company": company_id}
        )
        # Ainda dentro da transação: se o store ou o cofre falharem, nada do banco sai e o
        # comando pode ser repetido.
        removed = await store.delete_company(company_id) if store is not None else 0
        destroyed = {
            str(what): int(count)
            for what, count in (await destroyer.destroy(company_id) or {}).items()
        }
        await session.execute(
            insert(platform_events).values(
                action="empresa.apagada",
                actor=actor,
                target=f"empresa:{company_id}",
                data={
                    "empresa": str(company_id),
                    "nome_sha256": hashlib.sha256(row.name.encode()).hexdigest(),
                    "linhas": counts,
                    "artefatos": removed,
                    "chaves": destroyed,
                    **({"segredos_fora_do_openbao": secrets_left} if secrets_left else {}),
                    "pedido_por": actor,
                    "em": now_utc().isoformat(),
                },
            )
        )
    if secrets_left:
        logger.warning(
            "empresa %s apagada: o segredo de %d credencial(is) não estava no OpenBao e "
            "não foi apagado pelo RaPA",
            company_id,
            secrets_left,
        )
    folder = _remove_company_folder(files_dir, company_id)
    return PurgeReport(
        tables=counts,
        folders=[folder] if folder is not None else [],
        artifacts=removed,
        keys=destroyed,
    )
