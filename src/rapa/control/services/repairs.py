"""Consertar o passo que quebrou (:class:`~rapa.core.contracts.RepairService`).

Regras:

- **Quem pode.** Só quem tem papel na área do robô enxerga o conserto de uma
  execução (para outra área e outra empresa, a execução não existe). Ver os
  candidatos e a sugestão do modelo pede o papel de criação ou de aprovação na área;
  pedir o conserto (criar o rascunho), o de criação. Tudo passa por
  :meth:`Principal.has_role`, a função única das permissões, e a empresa vem sempre
  do token.
- **Só falhas do alvo.** Só uma execução que falhou (nunca uma indeterminada: depois
  de um passo irreversível, uma pessoa confere o sistema de destino antes) porque o
  alvo de um passo não foi encontrado, foi encontrado mais de uma vez ou só por um
  localizador alternativo num passo que grava, e que guardou o retrato da página
  (``run_snapshots``).
- **O modelo propõe, o código decide, a pessoa escolhe.** Os candidatos saem do
  retrato (``rapa.repair.enumerate_candidates``), sem executar nada de novo. O
  modelo (``rapa.llm.repair``) só escolhe um deles, ou nenhum, validado por esquema;
  com o hub fora do ar, lento demais (a espera é limitada, para caber no tempo da
  tela e do REST), sem hub ou com uma resposta inválida, a tela mostra os
  candidatos do mesmo jeito e a pessoa escolhe sozinha. A sugestão fica na memória do
  processo por uma hora (por execução e retrato), para não perguntar de novo a cada
  abertura da tela; cada pergunta ao modelo entra na auditoria.
- **Sempre um rascunho novo.** O conserto nunca muda uma versão publicada: ele cria a
  próxima versão do robô, em rascunho, com o alvo daquele passo trocado e mais nada
  (``rapa.repair.apply_candidate``), pelo mesmo caminho de toda versão
  (:func:`~rapa.control.services.robots.insert_version`: papel de criação, portão de
  política e auditoria). A versão é de quem pediu o conserto, que por isso nunca a
  publica; o modelo nunca é autor nem aprovador de nada. Publicar continua com outra
  pessoa, com o papel de aprovação (``PgRobotAdmin.publish``).
- **Um conserto por execução, sobre a versão mais nova.** A execução precisa ser da
  versão mais nova do robô (a publicada, ou um rascunho mais novo, numa validação):
  consertar uma versão antiga desfaria o que veio depois. Uma segunda tentativa para
  a mesma execução é conflito.
- **Certificado A1: desligado.** Robôs com certificado A1 nunca têm conserto
  assistido ("o conserto nunca é automático nessas sessões"): nem candidatos, nem
  modelo, nem rascunho (403). O conserto deles é uma gravação nova, acompanhada pelo
  custodiante (as regras do A1 chegam com a operação por empresa).
"""

import asyncio
import hashlib
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import RowMapping, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from rapa.control.db.schema import (
    repair_proposals,
    robot_versions,
    robots,
    run_snapshots,
    runs,
)
from rapa.control.db.session import Database
from rapa.control.services import audit
from rapa.control.services._common import PgService, ensure_company_active
from rapa.control.services.robots import insert_version
from rapa.core.contracts import (
    Conflict,
    Forbidden,
    InvalidInput,
    NotFound,
    Principal,
    RepairCandidate,
    RepairProposalInfo,
    RepairStepInfo,
    RepairSuggestion,
    RepairSuggestionStatus,
    RepairView,
    Role,
    RunState,
)
from rapa.core.ids import new_id
from rapa.core.robot import CredentialKind, Manifest
from rapa.core.snapshot import SNAPSHOT_ERROR_CODES, PageSnapshot
from rapa.llm.capabilities import CapabilityMatrix
from rapa.llm.client import HubClient
from rapa.llm.errors import LLMError, StructuredOutputError
from rapa.llm.repair import RepairAnswer, choose_candidate
from rapa.repair import (
    DEFAULT_CANDIDATES,
    NotRepairable,
    apply_candidate,
    enumerate_candidates,
    step_info,
)

__all__ = [
    "A1_DISABLED",
    "HubRepairAssistant",
    "PgRepairs",
    "RepairAssistant",
    "RepairAssistantUnavailable",
    "RepairChoiceRefused",
]

logger = logging.getLogger("rapa.control.repairs")

#: Por que o conserto assistido não vale para robôs com certificado A1.
A1_DISABLED = (
    "o conserto assistido fica desligado para robôs com certificado digital A1: o "
    "conserto deles nunca é automático e passa por uma gravação nova, acompanhada pelo "
    "custodiante"
)
_VIEW_ROLES = (Role.CREATOR, Role.APPROVER)
_SUGGESTION_TTL_S = 3600.0
#: Espera máxima pela sugestão do modelo: a tela monta em até 20 s e o REST espera 15 s.
SUGGESTION_TIMEOUT_S = 8.0


class RepairAssistantUnavailable(RuntimeError):
    """O modelo não pôde sugerir agora (hub fora do ar, matriz ausente; mensagem fixa)."""


class RepairChoiceRefused(RuntimeError):
    """A resposta do modelo não passou no esquema ou saiu da lista: o código recusou."""


class RepairAssistant(Protocol):
    async def choose(
        self,
        step: RepairStepInfo,
        candidates: Sequence[RepairCandidate],
        *,
        page_url: str,
        page_title: str,
        error_code: str,
    ) -> RepairAnswer: ...


class HubRepairAssistant:
    """A escolha do hub de LLM, com a matriz de capacidades medida pela sonda."""

    def __init__(
        self, client_factory: Callable[[], HubClient], capabilities_path: str | None
    ) -> None:
        self._client_factory = client_factory
        self._capabilities_path = capabilities_path

    async def choose(
        self,
        step: RepairStepInfo,
        candidates: Sequence[RepairCandidate],
        *,
        page_url: str,
        page_title: str,
        error_code: str,
    ) -> RepairAnswer:
        if self._capabilities_path is None:
            raise RepairAssistantUnavailable("a matriz de capacidades do hub não foi configurada")
        try:
            capabilities = CapabilityMatrix.load(self._capabilities_path)
        except OSError, ValueError:
            raise RepairAssistantUnavailable(
                "a matriz de capacidades do hub não pôde ser lida"
            ) from None
        try:
            async with self._client_factory() as client:
                return await choose_candidate(
                    client,
                    step,
                    candidates,
                    capabilities=capabilities,
                    page_url=page_url,
                    page_title=page_title,
                    error_code=error_code,
                )
        except StructuredOutputError:
            raise RepairChoiceRefused("a resposta do modelo não passou na conferência") from None
        except LLMError as exc:
            # Só o tipo: a mensagem pode trazer trechos da resposta do hub.
            logger.warning("o modelo não sugeriu um candidato: %s", type(exc).__name__)
            raise RepairAssistantUnavailable("o hub do modelo de linguagem não respondeu") from None


@dataclass(frozen=True)
class _Context:
    """Tudo o que o conserto de uma execução precisa, lido numa transação."""

    run: RowMapping
    manifest: Manifest
    snapshot: PageSnapshot
    snapshot_sha256: str
    step: RepairStepInfo
    candidates: list[RepairCandidate]
    proposal: RepairProposalInfo | None
    blocked_reason: str | None


def _proposal_info(row: RowMapping) -> RepairProposalInfo:
    return RepairProposalInfo(
        proposal_id=row["id"],
        run_id=row["run_id"],
        robot_id=row["robot_id"],
        step_id=row["step_id"],
        base_version_no=row["base_version_no"],
        version_no=row["version_no"],
        candidate_id=row["candidate_id"],
        model_candidate_id=row["model_candidate_id"],
        requested_by=row["requested_by"],
        created_at=row["created_at"],
    )


def _sha256(data: Any) -> str:
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


class PgRepairs(PgService):
    """O conserto assistido sobre o PostgreSQL (:class:`RepairService`)."""

    def __init__(
        self,
        db: Database,
        *,
        assistant: RepairAssistant | None = None,
        candidates: int = DEFAULT_CANDIDATES,
        suggestion_cache_size: int = 256,
        suggestion_timeout_s: float = SUGGESTION_TIMEOUT_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(db)
        self.assistant = assistant
        self.limit = candidates
        self.suggestion_timeout_s = suggestion_timeout_s
        self._cache: OrderedDict[tuple[UUID, UUID, str], tuple[float, RepairSuggestion]] = (
            OrderedDict()
        )
        self._cache_size = max(1, suggestion_cache_size)
        self._clock = clock

    # -- leitura ----------------------------------------------------------------

    async def _context(
        self, session: AsyncSession, principal: Principal, run_id: UUID, *, lock: bool
    ) -> _Context:
        company_id = principal.company_id
        run = (
            (
                await session.execute(
                    select(
                        runs.c.id,
                        runs.c.area_id,
                        runs.c.robot_id,
                        runs.c.version_id,
                        runs.c.state,
                        runs.c.error_code,
                        runs.c.error_message,
                        robots.c.slug,
                        robot_versions.c.version_no,
                        robot_versions.c.manifest,
                    )
                    .select_from(
                        runs.join(robots, robots.c.id == runs.c.robot_id).join(
                            robot_versions, robot_versions.c.id == runs.c.version_id
                        )
                    )
                    .where(runs.c.id == run_id, runs.c.company_id == company_id)
                )
            )
            .mappings()
            .one_or_none()
        )
        if run is None or not principal.roles_in(run["area_id"]):
            raise NotFound("execução não encontrada")
        if not any(principal.has_role(role, run["area_id"]) for role in _VIEW_ROLES):
            raise Forbidden("só quem cria ou aprova robôs nesta área conserta um robô")
        if lock:
            # Um conserto por vez em cada robô: a versão nova e a regra de um por execução.
            await session.execute(
                select(robots.c.id).where(robots.c.id == run["robot_id"]).with_for_update()
            )
        manifest = Manifest.model_validate(run["manifest"])
        if any(c.kind is CredentialKind.PFX_A1 for c in manifest.credentials):
            raise Forbidden(A1_DISABLED)
        state = RunState(run["state"])
        if state is RunState.INDETERMINATE:
            raise Conflict(
                "a execução ficou indeterminada depois de um passo irreversível: confira o "
                "sistema de destino antes; o conserto assistido só vale para execuções que "
                "falharam"
            )
        if state is not RunState.FAILED:
            raise Conflict("só uma execução que falhou pode ser consertada")
        if run["error_code"] not in SNAPSHOT_ERROR_CODES:
            raise Conflict(
                f"a falha desta execução ({run['error_code'] or 'sem código'}) não é de um "
                "elemento da página: o conserto assistido só vale quando o alvo de um passo "
                "não foi encontrado ou foi encontrado mais de uma vez"
            )
        row = (
            (
                await session.execute(
                    select(run_snapshots.c.snapshot, run_snapshots.c.snapshot_sha256)
                    .where(
                        run_snapshots.c.company_id == company_id,
                        run_snapshots.c.run_id == run_id,
                    )
                    .order_by(run_snapshots.c.attempt.desc())
                    .limit(1)
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise Conflict(
                "a execução não guardou o retrato da página no passo que falhou; rode o robô "
                "de novo para ter um retrato"
            )
        try:
            snapshot = PageSnapshot.model_validate(row["snapshot"])
        except ValidationError:
            raise Conflict(
                "o retrato da página desta execução está num formato que este servidor não lê"
            ) from None
        step = manifest.step_by_id(snapshot.step_id)
        if step is None:
            raise Conflict("o passo do retrato não existe na versão da execução")
        try:
            info = step_info(step)
            candidates = enumerate_candidates(snapshot, step, limit=self.limit)
        except NotRepairable as exc:
            raise Conflict(exc.message) from None
        proposal_row = (
            (
                await session.execute(
                    select(repair_proposals).where(
                        repair_proposals.c.company_id == company_id,
                        repair_proposals.c.run_id == run_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        proposal = _proposal_info(proposal_row) if proposal_row is not None else None
        blocked = await self._blocked_reason(session, run, proposal)
        return _Context(
            run=run,
            manifest=manifest,
            snapshot=snapshot,
            snapshot_sha256=str(row["snapshot_sha256"]),
            step=info,
            candidates=candidates,
            proposal=proposal,
            blocked_reason=blocked,
        )

    @staticmethod
    async def _blocked_reason(
        session: AsyncSession, run: RowMapping, proposal: RepairProposalInfo | None
    ) -> str | None:
        if proposal is not None:
            return (
                f"esta falha já tem um conserto: a versão {proposal.version_no}, pedida por "
                f"{proposal.requested_by}"
            )
        version_no = int(run["version_no"])
        newest = await session.scalar(
            select(func.max(robot_versions.c.version_no)).where(
                robot_versions.c.robot_id == run["robot_id"]
            )
        )
        if newest is not None and int(newest) != version_no:
            return (
                f"esta execução é da versão {version_no}, mas o robô já tem a versão {newest}: "
                "conserte a partir de uma falha da versão mais nova (valide-a ou publique-a e "
                "rode de novo)"
            )
        return None

    # -- sugestão do modelo ----------------------------------------------------------

    def _cached(self, key: tuple[UUID, UUID, str]) -> RepairSuggestion | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        stored_at, suggestion = entry
        if self._clock() - stored_at > _SUGGESTION_TTL_S:
            self._cache.pop(key, None)
            return None
        self._cache.move_to_end(key)
        return suggestion

    def _remember(self, key: tuple[UUID, UUID, str], suggestion: RepairSuggestion) -> None:
        self._cache[key] = (self._clock(), suggestion)
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    async def _ask(self, context: _Context) -> tuple[RepairSuggestion, bool]:
        """A sugestão do modelo (e se ele foi perguntado agora)."""
        if self.assistant is None:
            return (
                RepairSuggestion(
                    status=RepairSuggestionStatus.OFF,
                    message="Sem o hub do modelo de linguagem: escolha o candidato você mesmo.",
                ),
                False,
            )
        if not context.candidates:
            return (
                RepairSuggestion(
                    status=RepairSuggestionStatus.NONE,
                    message="Nenhum elemento da página serve a este passo; grave o robô de novo.",
                ),
                False,
            )
        run = context.run
        key = (run["robot_id"], run["id"], context.snapshot_sha256)
        cached = self._cached(key)
        if cached is not None:
            return cached, False
        try:
            # O hub pode não responder (pacotes descartados, fila cheia, Retry-After longo):
            # a tela não espera por ele mais do que cabe no tempo dela.
            async with asyncio.timeout(self.suggestion_timeout_s):
                answer = await self.assistant.choose(
                    context.step,
                    context.candidates,
                    page_url=context.snapshot.url,
                    page_title=context.snapshot.title,
                    error_code=context.snapshot.error_code,
                )
        except (RepairAssistantUnavailable, TimeoutError) as exc:
            if isinstance(exc, TimeoutError):
                logger.warning(
                    "o modelo não sugeriu um candidato em %.0f s", self.suggestion_timeout_s
                )
            # Não fica guardada: na próxima abertura da tela, o modelo é perguntado de novo.
            return (
                RepairSuggestion(
                    status=RepairSuggestionStatus.UNAVAILABLE,
                    message=(
                        "O modelo de linguagem não respondeu agora; escolha o candidato você "
                        "mesmo ou tente de novo mais tarde."
                    ),
                ),
                True,
            )
        except RepairChoiceRefused:
            suggestion = RepairSuggestion(
                status=RepairSuggestionStatus.REFUSED,
                message=(
                    "A resposta do modelo não passou na conferência do código e foi "
                    "descartada; escolha o candidato você mesmo."
                ),
            )
            self._remember(key, suggestion)
            return suggestion, True
        ids = {candidate.candidate_id for candidate in context.candidates}
        if answer.candidate_id is not None and answer.candidate_id not in ids:
            suggestion = RepairSuggestion(
                status=RepairSuggestionStatus.REFUSED,
                message="O modelo escolheu um elemento fora da lista; a sugestão foi descartada.",
            )
        elif answer.candidate_id is None:
            suggestion = RepairSuggestion(
                status=RepairSuggestionStatus.NONE,
                reason=answer.reason,
                message="O modelo acha que nenhum candidato serve; confira você mesmo.",
            )
        else:
            suggestion = RepairSuggestion(
                status=RepairSuggestionStatus.CHOSEN,
                candidate_id=answer.candidate_id,
                reason=answer.reason,
                message=(
                    f"O modelo sugere o candidato {answer.candidate_id}. Confira antes de "
                    "escolher: quem decide é você, e outra pessoa aprova a versão nova."
                ),
            )
        self._remember(key, suggestion)
        return suggestion, True

    # -- contrato ----------------------------------------------------------------------

    async def get_repair(
        self, principal: Principal, run_id: UUID, *, suggest: bool = True
    ) -> RepairView:
        async with self.db.tenant_session(principal.company_id) as session:
            context = await self._context(session, principal, run_id, lock=False)
        if suggest and context.blocked_reason is None:
            # Fora da transação: a resposta do hub pode demorar.
            suggestion, asked = await self._ask(context)
        else:
            suggestion = RepairSuggestion(
                status=RepairSuggestionStatus.OFF,
                message="A sugestão do modelo não foi pedida.",
            )
            asked = False
        if asked:
            async with self.db.tenant_session(principal.company_id) as session:
                await audit.append(
                    session,
                    company_id=principal.company_id,
                    actor=principal.subject,
                    action="conserto.sugestao_pedida",
                    target=f"execucao:{run_id}",
                    payload={
                        "passo": context.step.step_id,
                        "retrato": context.snapshot_sha256,
                        "situacao": suggestion.status.value,
                        "candidato": suggestion.candidate_id,
                    },
                )
        run = context.run
        return RepairView(
            run_id=run["id"],
            robot_id=run["robot_id"],
            robot_slug=run["slug"],
            area_id=run["area_id"],
            version_no=run["version_no"],
            error_code=run["error_code"],
            error_message=run["error_message"],
            page_url=context.snapshot.url,
            page_title=context.snapshot.title,
            step=context.step,
            candidates=context.candidates,
            suggestion=suggestion,
            truncated=context.snapshot.truncated,
            proposal=context.proposal,
            blocked_reason=context.blocked_reason,
        )

    async def create_repair(
        self, principal: Principal, run_id: UUID, candidate_id: str
    ) -> RepairProposalInfo:
        async with self.db.tenant_session(principal.company_id) as session:
            # Antes das travas das linhas: empresa congelada não pede conserto.
            await ensure_company_active(session, principal.company_id)
            context = await self._context(session, principal, run_id, lock=True)
            run = context.run
            if not principal.has_role(Role.CREATOR, run["area_id"]):
                raise Forbidden("só quem cria robôs nesta área pede um conserto")
            if context.blocked_reason is not None:
                raise Conflict(context.blocked_reason)
            candidate = next(
                (c for c in context.candidates if c.candidate_id == candidate_id), None
            )
            if candidate is None:
                raise InvalidInput(
                    "candidato desconhecido",
                    [f"escolha um dos candidatos listados ({len(context.candidates)})"],
                )
            try:
                repaired = apply_candidate(context.manifest, context.step.step_id, candidate)
            except NotRepairable as exc:
                raise InvalidInput("o conserto não pôde ser aplicado", [exc.message]) from None
            cached = self._cached((run["robot_id"], run["id"], context.snapshot_sha256))
            model_choice = (
                cached.candidate_id
                if cached is not None and cached.status is RepairSuggestionStatus.CHOSEN
                else None
            )
            version = await insert_version(session, principal, run["area_id"], repaired)
            if version.robot_id != run["robot_id"]:  # pragma: no cover - o nome não muda
                raise Conflict("o conserto criaria outro robô")
            row = (
                (
                    await session.execute(
                        insert(repair_proposals)
                        .values(
                            company_id=principal.company_id,
                            id=new_id(),
                            run_id=run_id,
                            robot_id=run["robot_id"],
                            step_id=context.step.step_id,
                            base_version_id=run["version_id"],
                            base_version_no=run["version_no"],
                            version_id=version.version_id,
                            version_no=version.version_no,
                            candidate_id=candidate.candidate_id,
                            candidate=candidate.model_dump(mode="json"),
                            model_candidate_id=model_choice,
                            snapshot_sha256=context.snapshot_sha256,
                            requested_by=principal.subject,
                        )
                        .returning(*repair_proposals.c)
                    )
                )
                .mappings()
                .one()
            )
            await audit.append(
                session,
                company_id=principal.company_id,
                actor=principal.subject,
                action="conserto.pedido",
                target=f"execucao:{run_id}",
                payload={
                    "robo": str(run["robot_id"]),
                    "passo": context.step.step_id,
                    "versao_base": run["version_no"],
                    "versao": version.version_no,
                    "candidato": candidate.candidate_id,
                    "alvo": _sha256(candidate.target.model_dump(mode="json")),
                    "modelo": model_choice,
                },
            )
        return _proposal_info(row)
