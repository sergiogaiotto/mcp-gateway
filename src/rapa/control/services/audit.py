"""Auditoria encadeada por empresa.

Cada linha de ``audit_log`` guarda o hash da anterior da mesma empresa
(``prev_hash``) e o próprio (``row_hash``), calculados pelo gatilho
``rapa_audit_chain`` no banco. Quem inclui só informa ator, ação, alvo e o
resumo (SHA-256) do conteúdo: o conteúdo em si não é guardado, então nada
sensível vai para a auditoria. :func:`verify_chain` refaz a cadeia em Python
com a mesma forma canônica do banco.
"""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from rapa.control.db.schema import audit_log

__all__ = [
    "GENESIS_HASH",
    "ChainReport",
    "append",
    "canonical_row",
    "payload_digest",
    "row_hash",
    "verify_chain",
]

#: ``prev_hash`` da primeira linha de cada empresa.
GENESIS_HASH = "0" * 64


def payload_digest(payload: Mapping[str, Any]) -> str:
    """SHA-256 da forma canônica (JSON ordenado) do conteúdo auditado."""
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


async def append(
    session: AsyncSession,
    *,
    company_id: UUID,
    actor: str,
    action: str,
    target: str,
    payload: Mapping[str, Any],
) -> None:
    """Inclui uma linha na auditoria da empresa (dentro da transação atual)."""
    await session.execute(
        insert(audit_log).values(
            company_id=company_id,
            actor=actor[:255],
            action=action,
            target=target,
            payload_digest=payload_digest(payload),
        )
    )


def _lp(value: str) -> str:
    return f"{len(value)}:{value};"


def canonical_row(
    *,
    prev_hash: str,
    company_id: UUID,
    seq: int,
    at: datetime,
    actor: str,
    action: str,
    target: str,
    payload_digest: str,
) -> str:
    """Mesma forma canônica da função SQL ``rapa_audit_hash``."""
    at_text = at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return "".join(
        _lp(part)
        for part in (
            prev_hash,
            str(company_id),
            str(seq),
            at_text,
            actor,
            action,
            target,
            payload_digest,
        )
    )


def row_hash(**fields: Any) -> str:
    return hashlib.sha256(canonical_row(**fields).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ChainReport:
    rows: int
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


async def verify_chain(session: AsyncSession, company_id: UUID) -> ChainReport:
    """Confere a cadeia de auditoria de uma empresa, linha por linha."""
    result = await session.execute(
        select(audit_log).where(audit_log.c.company_id == company_id).order_by(audit_log.c.seq)
    )
    problems: list[str] = []
    expected_prev = GENESIS_HASH
    expected_seq = 1
    rows = 0
    for row in result.mappings():
        rows += 1
        seq = int(row["seq"])
        if seq != expected_seq:
            problems.append(f"linha {seq}: esperava o número {expected_seq}")
        if row["prev_hash"] != expected_prev:
            problems.append(f"linha {seq}: o hash anterior não confere")
        computed = row_hash(
            prev_hash=row["prev_hash"],
            company_id=row["company_id"],
            seq=seq,
            at=row["at"],
            actor=row["actor"],
            action=row["action"],
            target=row["target"],
            payload_digest=row["payload_digest"],
        )
        if computed != row["row_hash"]:
            problems.append(f"linha {seq}: o hash da linha não confere")
        expected_prev = row["row_hash"]
        expected_seq = seq + 1
    return ChainReport(rows=rows, problems=problems)
