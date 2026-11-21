"""Hospedeiro falso (o mesmo canal, sem navegador), lançador falso e mundo de teste das sessões."""

import asyncio
import hmac
import json
import secrets
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.http11 import Request, Response

from rapa.control.sessions import (
    LiveRelay,
    MemorySessionStore,
    RecordingSessions,
    SessionEndpoint,
    SessionJob,
    SessionLimits,
    SessionNotStarted,
    TicketBook,
)
from rapa.core.contracts import Principal, Role
from rapa.core.ids import new_id
from rapa.fixtures.memory import MemoryPlatform
from rapa.recorder.models import Recording
from tests.compiler.builders import RecordingBuilder, button, password, textbox

#: Valores que nunca podem sair de uma sessão: a senha que a pessoa digitou na demonstração.
DEMO_PASSWORD = "Senha-Da-Demo-987"
FRAME = b"\xff\xd8quadro-falso"


def demo_recording(
    people: tuple[tuple[str, str], ...] = (("Maria", "529.982.247-25"),),
) -> Recording:
    """Login, depois o cadastro de cada pessoa (duas ou mais viram uma repetição)."""
    b = RecordingBuilder()
    b.navigate("/login")
    b.fill(textbox("Usuário", autocomplete="username"), None)
    b.fill(password(), None)
    b.click_to(button("Entrar"), "/inicio", method="POST")
    b.page_text("Início", text="Início\nCadastro de pessoas")
    b.navigate("/cadastro")
    b.page_text("Cadastro", text="Cadastro\nCadastro salvo com sucesso")
    for nome, cpf in people:
        b.fill(textbox("Nome"), nome)
        b.fill(textbox("CPF"), cpf)
        click = b.click(button("Salvar"))
        b.request("POST", "/cadastro", resource_type="fetch")
        del click
    return b.build()


class FakeHost:
    """Fala o protocolo do hospedeiro: token, uma conexão, quadros, entradas e ``parar``."""

    def __init__(self, recording: Recording) -> None:
        self.recording = recording
        self.token = secrets.token_urlsafe(32)
        self.received: list[dict[str, Any]] = []
        self.connections = 0
        self.alive = True
        self.server: Server | None = None
        self._client: ServerConnection | None = None
        self.stop_error: str | None = None

    @property
    def url(self) -> str:
        assert self.server is not None
        port = self.server.sockets[0].getsockname()[1]
        return f"ws://127.0.0.1:{port}/"

    def _authorize(self, connection: ServerConnection, request: Request) -> Response | None:
        header = request.headers.get("Authorization", "")
        if not hmac.compare_digest(header.encode(), f"Bearer {self.token}".encode()):
            return connection.respond(401, "nao autorizado\n")
        if self._client is not None:
            return connection.respond(409, "ocupada\n")
        return None

    async def start(self) -> None:
        self.server = await serve(
            self._handle, "127.0.0.1", 0, process_request=self._authorize, compression=None
        )

    async def _handle(self, connection: ServerConnection) -> None:
        self._client = connection
        self.connections += 1
        try:
            await connection.send(json.dumps({"tipo": "pronta", "largura": 1280, "altura": 800}))
            await connection.send(
                json.dumps(
                    {
                        "tipo": "pagina",
                        "url": "https://portal.exemplo.gov.br/login",
                        "titulo": "Entrar",
                    }
                )
            )
            await connection.send(FRAME)
            async for raw in connection:
                message = json.loads(raw)
                self.received.append(message)
                if message.get("tipo") == "parar":
                    if self.stop_error is not None:
                        await connection.send(
                            json.dumps(
                                {"tipo": "fim", "motivo": "erro", "mensagem": self.stop_error}
                            )
                        )
                    else:
                        await connection.send(
                            json.dumps(
                                {
                                    "tipo": "gravacao",
                                    "gravacao": self.recording.model_dump(mode="json"),
                                }
                            )
                        )
                        await connection.send(json.dumps({"tipo": "fim", "motivo": "parada"}))
                    self.alive = False
                    return
                if message.get("tipo") == "mouse":
                    await connection.send(FRAME + b"-" + str(len(self.received)).encode())
        finally:
            self._client = None

    async def emit(self, *events: dict[str, Any]) -> None:
        """Manda eventos ao plano de controle, como a página do hospedeiro mudando."""
        client = self._client
        assert client is not None
        for event in events:
            await client.send(json.dumps(event))

    async def end(self, reason: str) -> None:
        """Termina sozinho (sem atividade, prazo), como o hospedeiro de verdade."""
        self.alive = False
        client = self._client
        if client is not None:
            await client.send(json.dumps({"tipo": "fim", "motivo": reason}))
            await client.close()

    async def close(self) -> None:
        self.alive = False
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()


@dataclass
class FakeLauncher:
    """Lançador de hospedeiros falsos (um por sessão)."""

    recording: Recording = field(default_factory=demo_recording)
    hosts: dict[UUID, FakeHost] = field(default_factory=dict)
    jobs: list[SessionJob] = field(default_factory=list)
    fail: bool = False
    #: Sessões encerradas depois do "parar" (stop) e sem ele (terminate).
    stopped: list[UUID] = field(default_factory=list)
    terminated: list[UUID] = field(default_factory=list)

    async def start(self, job: SessionJob) -> SessionEndpoint:
        if self.fail:
            raise SessionNotStarted("não foi possível abrir o navegador da gravação")
        self.jobs.append(job)
        host = FakeHost(self.recording)
        await host.start()
        self.hosts[job.session_id] = host
        return SessionEndpoint(url=host.url, token=host.token)

    async def stop(self, session_id: UUID) -> None:
        self.stopped.append(session_id)
        host = self.hosts.get(session_id)
        if host is not None:
            await host.close()

    async def terminate(self, session_id: UUID) -> None:
        self.terminated.append(session_id)
        host = self.hosts.get(session_id)
        if host is not None:
            await host.close()

    async def is_alive(self, session_id: UUID) -> bool:
        host = self.hosts.get(session_id)
        return host is not None and host.alive

    async def aclose(self) -> None:
        for host in self.hosts.values():
            await host.close()


@dataclass
class SessionsWorld:
    """Duas empresas na plataforma em memória, com o serviço das sessões."""

    platform: MemoryPlatform
    store: MemorySessionStore
    launcher: FakeLauncher
    service: RecordingSessions
    company_a: UUID
    area_a: UUID
    other_area_a: UUID
    company_b: UUID
    area_b: UUID
    people: dict[str, Principal]


def build_world(
    *,
    limits: SessionLimits | None = None,
    assistant: Any = None,
    tickets: TicketBook | None = None,
    recording: Recording | None = None,
    reserve: int | None = 4,
) -> SessionsWorld:
    platform = MemoryPlatform()
    company_a, area_a, other_area_a = new_id(), new_id(), new_id()
    company_b, area_b = new_id(), new_id()
    store = MemorySessionStore(reserve=reserve)
    for company, area in ((company_a, area_a), (company_a, other_area_a), (company_b, area_b)):
        store.add_area(company, area)

    def person(company: UUID, subject: str, area: UUID | None, *roles: Role) -> Principal:
        if area is None:
            return Principal(subject=subject, company_id=company, company_roles=frozenset(roles))
        return Principal(subject=subject, company_id=company, area_roles={area: frozenset(roles)})

    people = {
        "criadora": person(company_a, "criadora@a", area_a, Role.CREATOR),
        "colega": person(company_a, "colega@a", area_a, Role.CREATOR),
        "aprovador": person(company_a, "aprovador@a", area_a, Role.APPROVER, Role.OPERATOR),
        "leitora": person(company_a, "leitora@a", area_a, Role.VIEWER),
        "vizinha": person(company_a, "vizinha@a", other_area_a, Role.CREATOR),
        "admin": person(company_a, "admin@a", None, Role.COMPANY_ADMIN),
        "outra": person(company_b, "criador@b", area_b, Role.CREATOR),
    }
    launcher = FakeLauncher(recording=recording or demo_recording())
    service = RecordingSessions(
        store,
        launcher,
        platform,
        limits=limits,
        assistant=assistant,
        relay=LiveRelay(stop_timeout_s=10),
        tickets=tickets,
    )
    return SessionsWorld(
        platform=platform,
        store=store,
        launcher=launcher,
        service=service,
        company_a=company_a,
        area_a=area_a,
        other_area_a=other_area_a,
        company_b=company_b,
        area_b=area_b,
        people=people,
    )


class QueueViewer:
    """Janela de teste: guarda o que recebe e entrega o que o teste manda."""

    def __init__(self) -> None:
        self.texts: list[dict[str, Any]] = []
        self.frames: list[bytes] = []
        self.inbox: asyncio.Queue[str | None] = asyncio.Queue()
        self.closed: tuple[int, str] | None = None
        self.changed = asyncio.Event()

    async def send_text(self, data: str) -> None:
        self.texts.append(json.loads(data))
        self.changed.set()

    async def send_bytes(self, data: bytes) -> None:
        self.frames.append(data)
        self.changed.set()

    async def receive_text(self) -> str | None:
        return await self.inbox.get()

    async def close(self, code: int, reason: str) -> None:
        self.closed = (code, reason)
        self.inbox.put_nowait(None)
        self.changed.set()

    def kinds(self) -> list[str]:
        return [t["tipo"] for t in self.texts]

    async def wait(self, check: Any, within: float = 5.0) -> None:
        async with asyncio.timeout(within):
            while not check():
                self.changed.clear()
                await self.changed.wait()
