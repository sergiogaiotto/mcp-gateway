"""Regras de admissão da fila: quem pode ganhar a próxima vaga de navegador.

Lógica pura (sem banco), usada por :func:`rapa.control.services.queue.claim_next`
dentro da transação que pega a execução.

- **Reserva interativa:** ``ceil(total × razão)`` vagas ficam para execuções
  interativas (gravação, controle ao vivo, testes e validação). Elas usam a
  reserva primeiro e, se faltar, qualquer vaga livre; não contam na cota da
  empresa.
- **Vagas garantidas:** abaixo da garantia, a empresa entra sempre que houver
  vaga fora da reserva.
- **Empréstimo:** acima da garantia e até ``garantia × fator``, a empresa só usa
  vagas realmente ociosas, isto é, sem contar as que as outras empresas ainda
  precisam para atender a própria garantia com o que têm na fila.
- **Teto:** execuções não interativas nunca passam de ``total − reserva``.
"""

import math
from collections.abc import Mapping
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
    #: Pode entrar uma execução interativa.
    interactive: bool
    #: Empresas abaixo da garantia que podem receber uma vaga agora.
    within_guarantee: frozenset[UUID]
    #: Empresas que podem pegar uma vaga ociosa emprestada.
    borrowing: frozenset[UUID]

    @property
    def anything(self) -> bool:
        return self.interactive or bool(self.within_guarantee) or bool(self.borrowing)


def plan(
    capacity: Capacity,
    loads: Mapping[UUID, CompanyLoad],
    interactive_running: int,
) -> AdmissionPlan:
    """Decide quem pode receber a próxima vaga."""
    noninteractive_running = sum(load.running for load in loads.values())
    total_running = noninteractive_running + interactive_running
    interactive_ok = total_running < capacity.total_slots
    # Execuções interativas ocupam a reserva primeiro; o que passar dela sai
    # das vagas gerais.
    overflow = max(0, interactive_running - capacity.reserve)
    free = capacity.general_pool - noninteractive_running - overflow
    if free <= 0:
        return AdmissionPlan(interactive_ok, frozenset(), frozenset())
    within: set[UUID] = set()
    borrowing: set[UUID] = set()
    total_unmet = sum(load.unmet_guarantee for load in loads.values())
    for company_id, load in loads.items():
        if load.queued <= 0:
            continue
        if load.running < load.guaranteed_slots:
            within.add(company_id)
        elif load.running < load.cap:
            others_unmet = total_unmet - load.unmet_guarantee
            if free - others_unmet >= 1:
                borrowing.add(company_id)
    return AdmissionPlan(interactive_ok, frozenset(within), frozenset(borrowing))
