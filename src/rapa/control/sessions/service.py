"""Regras das sessões de gravação: Demonstrar e Generalizar (:class:`RecordingSessions`).

Segue o protocolo :class:`rapa.core.contracts.RecordingService`. As regras:

- **Quem pode.** Só quem tem o papel de criação na área abre uma sessão
  (:meth:`Principal.has_role`, a mesma função de toda permissão). Só a dona da
  sessão, e enquanto tiver esse papel na área, a vê, recebe bilhete para a visão
  ao vivo, para e revisa; para qualquer outra pessoa a sessão não existe
  (``NotFound``). A empresa vem sempre do principal.
- **Vagas.** As sessões usam a reserva de vagas interativas da plataforma ("10%
  das vagas ficam reservadas para gravação, controle ao vivo e testes"): cada
  empresa tem no máximo ``per_company`` abertas (contadas no banco, sob trava) e
  este processo abre no máximo ``global_max`` (por padrão, o tamanho da reserva,
  lido de ``platform_settings``). O despachante também conta as sessões abertas na
  reserva, então uma validação não passa da reserva junto com elas.
- **Prazos.** Uma sessão sem entrada por ``idle_timeout_s`` (10 min) ou aberta há
  ``max_duration_s`` (60 min) termina sozinha e nada é guardado; a sessão cujo
  navegador sumiu também termina. A situação é atualizada quando o hospedeiro
  avisa o fim, a cada consulta e na varredura periódica (:meth:`sweep`).
- **Parar.** O hospedeiro para o gravador e devolve a gravação, que o gravador já
  limpou de segredos; ela é validada (:class:`rapa.recorder.Recording`) e guardada
  com a sessão parada e a auditoria, na mesma transação.
- **Generalizar.** As propostas vêm da compilação determinística da gravação e,
  se o hub estiver de pé, do modelo (:mod:`rapa.compiler.review`); sem o hub, as
  sugestões das regras continuam lá, com um aviso. O rascunho aplica só o que a
  pessoa confirmou e entra pelo serviço de robôs (:class:`RobotAdmin`), com toda a
  validação e o portão de política de sempre; o criador da versão é a dona da
  sessão, que por isso nunca a aprova.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from rapa.compiler import CompileError, lower
from rapa.compiler.naming import slugify
from rapa.compiler.review import apply_choices, build_proposals
from rapa.control.sessions.assistant import AssistantUnavailable, GeneralizationAssistant
from rapa.control.sessions.launcher import SessionJob, SessionLauncher, SessionNotStarted
from rapa.control.sessions.relay import LiveRelay, SessionGone, StopFailed, Viewer
from rapa.control.sessions.store import LIVE_STATUSES, SessionRecord, SessionStore
from rapa.control.sessions.tickets import TicketBook
from rapa.core.contracts import (
    MAX_SESSIONS_LISTED,
    Conflict,
    DraftChoices,
    Forbidden,
    GeneralizationProposals,
    InvalidInput,
    NotFound,
    Principal,
    QuotaExceeded,
    RecordingSessionInfo,
    RecordingSessionStatus,
    RobotAdmin,
    RobotVersionInfo,
    Role,
    ServiceUnavailable,
    ViewerTicket,
)
from rapa.core.edits import EditOp
from rapa.core.ids import new_id
from rapa.recorder.models import Recording
from rapa.session.protocol import check_web_url, dump_event

__all__ = ["RecordingSessions", "SessionLimits", "default_robot_info"]

logger = logging.getLogger("rapa.control.sessions")

_MODEL_OFF = (
    "O modelo de linguagem não está configurado neste servidor; confirme as sugestões "
    "automáticas abaixo."
)
_MODEL_DOWN = (
    "As propostas do modelo estão indisponíveis agora (o hub não respondeu). Você ainda pode "
    "confirmar as sugestões automáticas; tente de novo mais tarde para ver as do modelo."
)
#: Quanto tempo uma falha do modelo fica guardada antes de perguntar de novo.
_MODEL_RETRY_S = 60.0
#: Uma sessão "iniciando" há mais que isto, fora deste processo, não vai abrir.
_STALE_START = timedelta(minutes=5)


@dataclass(frozen=True)
class SessionLimits:
    """Limites e prazos das sessões de gravação."""

    #: Sessões abertas ao mesmo tempo por empresa.
    per_company: int = 2
    #: Sessões abertas ao mesmo tempo neste processo; None usa o tamanho da reserva interativa.
    global_max: int | None = None
    idle_timeout_s: float = 600.0
    max_duration_s: float = 3600.0
    width: int = 1280
    height: int = 800
    #: Espera do modelo de linguagem pelas propostas.
    model_timeout_s: float = 90.0

    def __post_init__(self) -> None:
        if self.per_company < 1:
            raise ValueError("cada empresa precisa de pelo menos uma vaga de gravação")
        if self.global_max is not None and self.global_max < 1:
            raise ValueError("o limite global de gravações precisa ser positivo")


def default_robot_info(record: SessionRecord) -> tuple[str, str, str]:
    """Nome técnico, título e descrição sugeridos para o robô da sessão."""
    title = record.title.strip() or "Robô gravado"
    slug = slugify(title, max_words=6, max_len=50) or "robo_gravado"
    if not slug[0].isalpha():
        slug = f"robo_{slug}"
    if len(slug) < 3:
        slug = f"robo_{slug}"
    when = record.created_at.astimezone(UTC).strftime("%d/%m/%Y")
    description = f"Robô gravado numa demonstração em {when}."
    return slug[:64], title[:120], description


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class _ModelAnswer:
    ops: list[EditOp] | None
    message: str | None
    at: float


class RecordingSessions:
    """Sessões de gravação, visão ao vivo e revisão (:class:`RecordingService`)."""

    def __init__(
        self,
        store: SessionStore,
        launcher: SessionLauncher,
        robots: RobotAdmin,
        *,
        limits: SessionLimits | None = None,
        assistant: GeneralizationAssistant | None = None,
        relay: LiveRelay | None = None,
        tickets: TicketBook | None = None,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.store = store
        self.launcher = launcher
        self.robots = robots
        self.limits = limits or SessionLimits()
        self.assistant = assistant
        self.relay = relay or LiveRelay()
        if self.relay.on_end is None:
            self.relay.on_end = self._host_ended
        self.tickets = tickets or TicketBook()
        self._clock = clock
        #: Sessões abertas por este processo: sessão → empresa.
        self._live: dict[UUID, UUID] = {}
        self._lock = asyncio.Lock()
        self._model: dict[tuple[UUID, UUID], _ModelAnswer] = {}

    def __repr__(self) -> str:
        return f"RecordingSessions(abertas={len(self._live)})"

    # -- abrir -----------------------------------------------------------------

    async def create_session(
        self, principal: Principal, area_id: UUID, start_url: str, title: str = ""
    ) -> RecordingSessionInfo:
        if not principal.has_role(Role.CREATOR, area_id):
            raise Forbidden("só quem cria robôs nesta área pode gravar uma demonstração")
        try:
            url = check_web_url(start_url)
        except ValueError as exc:
            raise InvalidInput("endereço inicial inválido", [str(exc)]) from None
        title = " ".join(title.split())[:120]
        company_id = principal.company_id
        if not await self.store.area_exists(company_id, area_id):
            raise NotFound("área não encontrada")
        now = self._clock()
        limits = self.limits
        record = SessionRecord(
            company_id=company_id,
            session_id=new_id(),
            area_id=area_id,
            owner=principal.subject,
            title=title,
            start_url=url,
            status=RecordingSessionStatus.STARTING,
            created_at=now,
            expires_at=now + timedelta(seconds=limits.max_duration_s),
        )
        async with self._lock:
            for live in await self.store.list_live(company_id):
                await self._refresh(live)
            cap = limits.global_max or await self.store.interactive_reserve() or 1
            if len(self._live) >= cap:
                # Antes de recusar, solta as vagas das sessões que já terminaram sozinhas.
                await self.sweep()
            if len(self._live) >= cap:
                raise QuotaExceeded(
                    "todas as vagas de gravação da plataforma estão em uso; tente de novo em "
                    "instantes",
                    30,
                )
            await self.store.insert(record, company_limit=limits.per_company)
            self._live[record.session_id] = company_id
        try:
            endpoint = await self.launcher.start(
                SessionJob(
                    session_id=record.session_id,
                    company_id=company_id,
                    start_url=url,
                    idle_timeout_s=limits.idle_timeout_s,
                    max_duration_s=limits.max_duration_s,
                    width=limits.width,
                    height=limits.height,
                )
            )
        except SessionNotStarted as exc:
            await self._end(record, RecordingSessionStatus.FAILED, "nao_iniciou")
            raise ServiceUnavailable(f"{exc}; tente de novo em instantes") from None
        except BaseException:
            await self._end(record, RecordingSessionStatus.FAILED, "nao_iniciou")
            raise
        self.relay.register(record.session_id, endpoint)
        started = await self.store.set_status(
            company_id,
            record.session_id,
            expected={RecordingSessionStatus.STARTING},
            status=RecordingSessionStatus.ACTIVE,
            started_at=self._clock(),
        )
        if started is None:
            # Encerrada enquanto abria (o prazo, o fim do serviço): o navegador não fica solto.
            self._live.pop(record.session_id, None)
            with contextlib.suppress(Exception):
                await self.launcher.stop(record.session_id)
            await self.relay.forget(record.session_id)
            raise Conflict("a sessão de gravação terminou antes de abrir")
        return started.info()

    # -- consultar -------------------------------------------------------------

    async def _owned(self, principal: Principal, session_id: UUID) -> SessionRecord:
        record = await self.store.get(principal.company_id, session_id)
        if (
            record is None
            or record.owner != principal.subject
            or not principal.has_role(Role.CREATOR, record.area_id)
        ):
            raise NotFound("sessão de gravação não encontrada")
        return await self._refresh(record)

    async def get_session(
        self, principal: Principal, session_id: UUID
    ) -> RecordingSessionInfo | None:
        try:
            return (await self._owned(principal, session_id)).info()
        except NotFound:
            return None

    async def list_sessions(
        self, principal: Principal, limit: int = 20
    ) -> list[RecordingSessionInfo]:
        limit = max(1, min(limit, MAX_SESSIONS_LISTED))
        records = await self.store.list_owned(principal.company_id, principal.subject, limit)
        out: list[RecordingSessionInfo] = []
        for record in records:
            if principal.has_role(Role.CREATOR, record.area_id):
                out.append((await self._refresh(record)).info())
        return out

    async def _refresh(self, record: SessionRecord) -> SessionRecord:
        """Confere se uma sessão aberta ainda tem navegador e prazo; encerra se não."""
        if record.status not in LIVE_STATUSES:
            return record
        now = self._clock()
        if now >= record.expires_at:
            return await self._end(record, RecordingSessionStatus.EXPIRED, "tempo_esgotado")
        if (
            record.status is RecordingSessionStatus.STARTING
            and record.session_id not in self._live
            and now - record.created_at > _STALE_START
        ):
            # Ficou "iniciando" num processo que parou no meio da abertura.
            return await self._end(record, RecordingSessionStatus.FAILED, "nao_iniciou")
        if record.status is RecordingSessionStatus.ACTIVE and not await self.launcher.is_alive(
            record.session_id
        ):
            return await self._end(record, RecordingSessionStatus.EXPIRED, "encerrada")
        return record

    async def _end(
        self, record: SessionRecord, status: RecordingSessionStatus, reason: str
    ) -> SessionRecord:
        self._live.pop(record.session_id, None)
        self.tickets.revoke(record.session_id)
        with contextlib.suppress(Exception):
            await self.launcher.stop(record.session_id)
        await self.relay.forget(record.session_id)
        updated = await self.store.set_status(
            record.company_id,
            record.session_id,
            expected=LIVE_STATUSES,
            status=status,
            end_reason=reason,
            stopped_at=self._clock(),
        )
        if updated is None:
            current = await self.store.get(record.company_id, record.session_id)
            return current or record
        return updated

    async def _host_ended(self, session_id: UUID, reason: str) -> None:
        """O hospedeiro avisou o fim (sem atividade, prazo, sinal) ou a ligação caiu."""
        company_id = self._live.get(session_id)
        if company_id is None or reason == "parada":
            return
        record = await self.store.get(company_id, session_id)
        if record is None or record.status not in LIVE_STATUSES:
            return
        if reason not in {"ociosa", "tempo_esgotado", "encerrada", "erro"}:
            # A ligação caiu: só encerra se o navegador também sumiu.
            if await self.launcher.is_alive(session_id):
                return
            reason = "perdida"
        status = (
            RecordingSessionStatus.FAILED if reason == "erro" else RecordingSessionStatus.EXPIRED
        )
        await self._end(record, status, reason)

    async def sweep(self) -> int:
        """Encerra as sessões deste processo cujo navegador sumiu ou cujo prazo acabou."""
        ended = 0
        for session_id, company_id in list(self._live.items()):
            record = await self.store.get(company_id, session_id)
            if record is None:
                self._live.pop(session_id, None)
                continue
            refreshed = await self._refresh(record)
            if refreshed.status not in LIVE_STATUSES:
                ended += 1
        return ended

    async def aclose(self) -> None:
        """Encerra as sessões abertas por este processo (no fim do serviço)."""
        for session_id, company_id in list(self._live.items()):
            record = await self.store.get(company_id, session_id)
            if record is not None:
                with contextlib.suppress(Exception):
                    await self._end(record, RecordingSessionStatus.EXPIRED, "encerrada")
        await self.relay.aclose()

    # -- visão ao vivo -----------------------------------------------------------

    async def issue_ticket(self, principal: Principal, session_id: UUID) -> ViewerTicket:
        record = await self._owned(principal, session_id)
        if record.status is not RecordingSessionStatus.ACTIVE:
            raise Conflict("a sessão de gravação não está aberta")
        if not self.relay.knows(session_id):
            raise Conflict("a sessão de gravação não está aberta neste servidor")
        ticket, _ = self.tickets.issue(session_id, principal)
        return ViewerTicket(
            session_id=session_id,
            ticket=ticket,
            expires_at=self._clock() + timedelta(seconds=self.tickets.ttl_s),
            path=f"/v1/sessoes/{session_id}/ao-vivo",
        )

    async def admit_viewer(self, session_id: UUID, ticket: str) -> str:
        """Resgata o bilhete e confere a sessão; devolve o evento ``sessao`` para a janela.

        Levanta ``NotFound`` (bilhete inválido, vencido, já usado ou de outra sessão; a
        sessão não é mais da pessoa) ou ``Conflict`` (a sessão não está aberta).
        """
        grant = self.tickets.redeem(ticket, session_id)
        if grant is None:
            raise NotFound("bilhete inválido ou vencido")
        record = await self._owned(grant.principal, session_id)
        if record.status is not RecordingSessionStatus.ACTIVE or not self.relay.knows(session_id):
            raise Conflict("a sessão de gravação não está aberta")
        return dump_event(
            "sessao",
            expira_em=record.expires_at.isoformat(),
            restante_s=max(0, int((record.expires_at - self._clock()).total_seconds())),
            ociosidade_s=int(self.limits.idle_timeout_s),
        )

    async def relay_viewer(self, session_id: UUID, viewer: Viewer, greeting: str) -> None:
        """Repassa a visão ao vivo para a janela admitida até ela sair ou a sessão acabar."""
        try:
            await self.relay.watch(session_id, viewer, greeting=greeting)
        except SessionGone:
            company_id = self._live.get(session_id)
            record = await self.store.get(company_id, session_id) if company_id else None
            if record is not None:
                await self._refresh(record)
            raise Conflict("o navegador da gravação não responde") from None

    # -- parar -------------------------------------------------------------------

    async def stop_session(self, principal: Principal, session_id: UUID) -> RecordingSessionInfo:
        record = await self._owned(principal, session_id)
        if record.status is RecordingSessionStatus.STARTING:
            raise Conflict("o navegador da gravação ainda está abrindo; tente de novo em instantes")
        if record.status not in LIVE_STATUSES:
            raise Conflict("a sessão de gravação já terminou")
        if not self.relay.knows(session_id):
            await self._end(record, RecordingSessionStatus.EXPIRED, "perdida")
            raise Conflict("a sessão de gravação não está aberta neste servidor; grave de novo")
        try:
            payload = await self.relay.stop(session_id)
        except (SessionGone, StopFailed) as exc:
            await self._end(record, RecordingSessionStatus.FAILED, "parada_falhou")
            raise Conflict(f"não foi possível fechar a gravação: {exc}") from None
        try:
            recording = Recording.model_validate(payload)
        except ValidationError:
            logger.error("o hospedeiro devolveu uma gravação inválida")
            await self._end(record, RecordingSessionStatus.FAILED, "gravacao_invalida")
            raise Conflict("o navegador devolveu uma gravação inválida; grave de novo") from None
        self.tickets.revoke(session_id)
        saved = await self.store.save_recording(
            record.company_id,
            session_id,
            recording.model_dump(mode="json"),
            actor=principal.subject,
            stopped_at=self._clock(),
        )
        self._live.pop(session_id, None)
        with contextlib.suppress(Exception):
            await self.launcher.stop(session_id)
        await self.relay.forget(session_id)
        return saved.info()

    # -- generalizar -------------------------------------------------------------

    async def _recording(
        self, principal: Principal, session_id: UUID
    ) -> tuple[SessionRecord, UUID, Recording]:
        record = await self._owned(principal, session_id)
        if not record.status.reviewable:
            raise Conflict("a sessão de gravação ainda não tem gravação para revisar")
        found = await self.store.get_recording(record.company_id, session_id)
        if found is None:  # pragma: no cover - a gravação entra com a sessão parada
            raise NotFound("gravação não encontrada")
        recording_id, data = found
        return record, recording_id, Recording.model_validate(data)

    async def _model_ops(
        self, key: tuple[UUID, UUID], manifest: Any, observed: set[str]
    ) -> tuple[list[EditOp] | None, str | None]:
        loop = asyncio.get_running_loop()
        cached = self._model.get(key)
        if cached is not None and (
            cached.ops is not None or loop.time() - cached.at < _MODEL_RETRY_S
        ):
            return cached.ops, cached.message
        if self.assistant is None:
            return None, _MODEL_OFF
        try:
            ops: list[EditOp] | None = await asyncio.wait_for(
                self.assistant.propose(manifest, observed), timeout=self.limits.model_timeout_s
            )
            message = None
        except AssistantUnavailable, TimeoutError:
            ops, message = None, _MODEL_DOWN
        except Exception as exc:
            logger.warning("o modelo falhou nas propostas: %s", type(exc).__name__)
            ops, message = None, _MODEL_DOWN
        if len(self._model) > 256:
            self._model.clear()
        self._model[key] = _ModelAnswer(ops, message, loop.time())
        return ops, message

    async def get_proposals(
        self, principal: Principal, session_id: UUID
    ) -> GeneralizationProposals:
        record, recording_id, recording = await self._recording(principal, session_id)
        name, title, description = default_robot_info(record)
        try:
            compiled = lower(recording, name=name, title=title, description=description)
        except CompileError as exc:
            raise InvalidInput("a gravação não pôde virar um robô", [str(exc)]) from None
        ops, message = await self._model_ops(
            (session_id, recording_id), compiled.manifest, compiled.observed_texts
        )
        return build_proposals(
            compiled,
            session_id=session_id,
            recording_id=recording_id,
            model_ops=ops,
            model_message=message,
        )

    async def create_draft(
        self, principal: Principal, session_id: UUID, choices: DraftChoices
    ) -> RobotVersionInfo:
        record, _recording_id, recording = await self._recording(principal, session_id)
        try:
            compiled = lower(
                recording, name=choices.name, title=choices.title, description=choices.description
            )
        except CompileError as exc:
            raise InvalidInput("a gravação não pôde virar um robô", [str(exc)]) from None
        manifest = apply_choices(compiled, choices)
        version = await self.robots.create_version(principal, record.area_id, manifest)
        await self.store.mark_drafted(
            record.company_id,
            session_id,
            robot_id=version.robot_id,
            version_no=version.version_no,
            actor=principal.subject,
        )
        return version
