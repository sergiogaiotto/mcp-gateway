"""Regras de admissão da fila: quem pode ganhar a próxima vaga de navegador.

Lógica pura (sem banco), usada por :func:`rapa.control.services.queue.claim_next`
dentro da transação que pega a execução.

- **Reserva interativa:** ``ceil(total × razão)`` vagas ficam para execuções
  interativas (gravação, controle ao vivo e testes: a validação e as sessões de
  gravação, que contam como interativas em uso enquanto estão abertas). Elas
  usam a reserva primeiro e não contam na cota da empresa. Com a reserva
  cheia, um interativo só usa uma vaga geral que nenhuma outra empresa precise
  para atender a própria garantia com o que tem na fila: interativos de uma
  empresa nunca tomam as vagas garantidas das outras.
- **Vagas garantidas:** abaixo da garantia, a empresa entra sempre que houver
  vaga fora da reserva.
- **Empréstimo:** acima da garantia e até ``garantia × fator``, a empresa só usa
  vagas realmente ociosas, isto é, sem contar as que as outras empresas ainda
  precisam para atender a própria garantia com o que têm na fila.
- **Teto:** execuções não interativas nunca passam de ``total − reserva``.
"""

import math
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

__all__ = ["AdmissionPlan", "Capacity", "CompanyLoad", "plan"]


@dataclass(frozen=True, slots=True)
class Capacity:
    total_slots: int
    interactive_reserve_ratio: Decimal

    @property
    def reserve(self) -> int:
        if self.interactive_reserve_ratio <= 0:
            return 0
        return min(self.total_slots, math.ceil(self.total_slots * self.interactive_reserve_ratio))

    @property
    def general_pool(self) -> int:
        """Vagas para execuções não interativas."""
        return self.total_slots - self.reserve


@dataclass(frozen=True, slots=True)
class CompanyLoad:
    """Uso de uma empresa, só com execuções não interativas."""

    running: int
    queued: int
    guaranteed_slots: int = 0
    max_borrow_factor: Decimal = Decimal(1)

    @property
    def cap(self) -> int:
        """Teto com empréstimo: ``garantia × fator`` (nunca abaixo da garantia)."""
        borrowed = math.floor(Decimal(self.guaranteed_slots) * self.max_borrow_factor)
        return max(self.guaranteed_slots, borrowed)

    @property
    def unmet_guarantee(self) -> int:
        """Vagas da garantia que a empresa ainda vai usar com o que tem na fila."""
        return max(0, min(self.guaranteed_slots - self.running, self.queued))


@dataclass(frozen=True, slots=True)
class AdmissionPlan:
    #: Pode entrar uma execução interativa de qualquer empresa (há vaga na reserva).
    interactive: bool
    #: Empresas abaixo da garantia que podem receber uma vaga agora.
    within_guarantee: frozenset[UUID]
    #: Empresas que podem pegar uma vaga ociosa emprestada.
    borrowing: frozenset[UUID]
    #: Com a reserva cheia, empresas cujo interativo pode usar uma vaga geral.
    interactive_overflow: frozenset[UUID] = frozenset()

    @property
    def anything(self) -> bool:
        return (
            self.interactive
            or bool(self.interactive_overflow)
            or bool(self.within_guarantee)
            or bool(self.borrowing)
        )


def plan(
    capacity: Capacity,
    loads: Mapping[UUID, CompanyLoad],
    interactive_running: int,
    interactive_waiting: Collection[UUID] = (),
) -> AdmissionPlan:
    """Decide quem pode receber a próxima vaga.

    ``interactive_waiting`` são as empresas com execução interativa na fila.
    """
    noninteractive_running = sum(load.running for load in loads.values())
    total_running = noninteractive_running + interactive_running
    room = total_running < capacity.total_slots
    interactive_ok = room and interactive_running < capacity.reserve
    # Execuções interativas ocupam a reserva primeiro; o que passar dela sai
    # das vagas gerais.
    overflow = max(0, interactive_running - capacity.reserve)
    free = capacity.general_pool - noninteractive_running - overflow
    if free <= 0:
        return AdmissionPlan(interactive_ok, frozenset(), frozenset())
    total_unmet = sum(load.unmet_guarantee for load in loads.values())

    def others_unmet(company_id: UUID) -> int:
        own = loads[company_id].unmet_guarantee if company_id in loads else 0
        return total_unmet - own

    interactive_overflow: set[UUID] = set()
    if room and not interactive_ok:
        # A vaga pode ser da garantia da própria empresa, nunca da de outra.
        interactive_overflow = {
            company_id for company_id in interactive_waiting if free - others_unmet(company_id) >= 1
        }
    within: set[UUID] = set()
    borrowing: set[UUID] = set()
    for company_id, load in loads.items():
        if load.queued <= 0:
            continue
        if load.running < load.guaranteed_slots:
            within.add(company_id)
        elif load.running < load.cap and free - others_unmet(company_id) >= 1:
            borrowing.add(company_id)
    return AdmissionPlan(
        interactive_ok, frozenset(within), frozenset(borrowing), frozenset(interactive_overflow)
    )
