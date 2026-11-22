"""O proxy de saída com usuário e senha (``RAPA_PROXY_SAIDA`` vindo de um Secret, como o chart
oferece): a partida aceita, o ``CONNECT`` leva ``Proxy-Authorization: Basic`` e a senha nunca vai
para uma linha de comando, um ``repr`` ou uma mensagem."""

import asyncio
import base64
import contextlib
import json
import socket
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from rapa.control.launcher import LocalLauncherSettings, LocalProcessLauncher
from rapa.core.contracts import RunnerJob
from rapa.core.ids import new_id
from rapa.fixtures.robots import emitir_protocolo
from rapa.runner import worker
from rapa.runner.pod import PodOptions, _worker_command, _worker_env
from rapa.server import ServerSettings
from rapa.session.egress import PROXY_CREDENTIALS_ENV, EgressGuard, EgressPolicy, UpstreamProxy
from rapa.session.host import HostConfig
from tests.control.launcher.test_launcher import RecordingIngestor
from tests.session.test_egress import _socks, echo_server, running_guard

SENHA = "Tr0ub4dor@&:3"
#: A senha com os caracteres reservados escapados, como vai na URL do Secret.
URL = "http://usuario:Tr0ub4dor%40%26%3A3@proxy.interno:3128"
BASIC = "Basic " + base64.b64encode(f"usuario:{SENHA}".encode()).decode()


def test_the_password_is_parsed_and_never_shown() -> None:
    proxy = UpstreamProxy.parse(URL)
    assert (proxy.host, proxy.port, proxy.url) == (
        "proxy.interno",
        3128,
        "http://proxy.interno:3128",
    )
    assert proxy.authorization == BASIC
    assert UpstreamProxy.parse(proxy.url_with_credentials) == proxy
    policy = EgressPolicy.parse(["10.20.0.0/16"], URL)
    for text in (repr(proxy), repr(policy), " ".join(policy.worker_args())):
        assert "Tr0ub4dor" not in text
    # O worker recebe usuário e senha pelo ambiente, nunca pela linha de comando.
    assert policy.worker_env() == {PROXY_CREDENTIALS_ENV: f"usuario:{SENHA}"}
    assert EgressPolicy.parse((), "http://proxy.interno:3128").worker_env() == {}
    rebuilt = EgressPolicy.parse(
        ["10.20.0.0/16"], "http://proxy.interno:3128", proxy_credentials=f"usuario:{SENHA}"
    )
    assert rebuilt == policy


@pytest.mark.parametrize(
    "url",
    ["http://:Tr0ub4dor@proxy.interno:3128", "http://usuario:Tr0ub4dor%0d%0a@proxy.interno:3128"],
)
def test_a_bad_user_or_password_is_refused_without_repeating_it(url: str) -> None:
    with pytest.raises(ValueError) as caught:
        UpstreamProxy.parse(url)
    assert "Tr0ub4dor" not in str(caught.value)


def test_the_server_starts_with_the_proxy_from_the_secret(tmp_path: Path) -> None:
    from tests.e2e.test_server_pods import _dev

    settings = ServerSettings.from_env({**_dev(tmp_path), "RAPA_PROXY_SAIDA": URL})
    proxy = settings.egress_policy().upstream_proxy
    assert proxy is not None and proxy.authorization == BASIC
    assert "Tr0ub4dor" not in repr(settings)


@asynccontextmanager
async def authenticated_proxy(target_port: int) -> AsyncIterator[tuple[int, list[bytes]]]:
    """Um proxy CONNECT que exige ``Proxy-Authorization: Basic`` (407 sem ele)."""
    heads: list[bytes] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(Exception):
            head = await reader.readuntil(b"\r\n\r\n")
            heads.append(head)
            lines = head.decode().split("\r\n")
            if f"Proxy-Authorization: {BASIC}" not in lines:
                writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
                await writer.drain()
                writer.close()
                return
            up_reader, up_writer = await asyncio.open_connection("127.0.0.1", target_port)
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()

            async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
                with contextlib.suppress(Exception):
                    while data := await src.read(1024):
                        dst.write(data)
                        await dst.drain()
                    dst.write_eof()

            await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield int(server.sockets[0].getsockname()[1]), heads
    finally:
        server.close()
        server.close_clients()


async def _no_dns(name: str) -> list[str]:
    raise socket.gaierror("sem nome")


async def test_the_connect_carries_the_proxy_authorization() -> None:
    async with echo_server() as target, authenticated_proxy(target) as (proxy_port, heads):
        url = f"http://usuario:Tr0ub4dor%40%26%3A3@127.0.0.1:{proxy_port}"
        guard = EgressGuard(resolver=_no_dns, policy=EgressPolicy.parse((), url))
        async with running_guard(guard) as port:
            code, reader, writer = await _socks(port, "portal.exemplo", 443)
            assert code == 0x00
            writer.write(b"ola")
            await writer.drain()
            assert await reader.readexactly(3) == b"ola"
            writer.close()
        # Sem usuário e senha, o proxy recusa (407): a página recebe "proibido".
        plain = EgressGuard(
            resolver=_no_dns, policy=EgressPolicy.parse((), f"http://127.0.0.1:{proxy_port}")
        )
        async with running_guard(plain) as port:
            code, _, writer = await _socks(port, "portal.exemplo", 443)
            writer.close()
            assert code == 0x02
    assert heads[0].startswith(b"CONNECT portal.exemplo:443 HTTP/1.1\r\n")


def _job() -> RunnerJob:
    return RunnerJob(
        run_id=new_id(),
        company_id=new_id(),
        attempt=1,
        manifest=emitir_protocolo("http://portal-v1.rapa.test:8080", "portal-v1.rapa.test"),
        inputs={},
        deadline=datetime.now(UTC) + timedelta(minutes=2),
    )


def test_the_local_worker_gets_the_password_in_the_environment(tmp_path: Path) -> None:
    launcher = LocalProcessLauncher(
        RecordingIngestor(),
        LocalLauncherSettings(
            work_dir=tmp_path / "t",
            artifacts_dir=tmp_path / "a",
            egress=EgressPolicy.parse((), URL),
        ),
    )
    job = _job()
    work, artifacts = launcher._paths(job)
    command = launcher._command(job, work, artifacts, with_secrets=False)
    assert "--proxy-saida=http://proxy.interno:3128" in command
    assert not any("Tr0ub4dor" in part for part in command)
    assert launcher._env()[PROXY_CREDENTIALS_ENV] == f"usuario:{SENHA}"


def test_the_pod_worker_gets_the_password_in_the_environment(tmp_path: Path) -> None:
    from rapa.control.channel import Channel
    from rapa.control.launcher import KubernetesLauncher
    from tests.control.launcher.fake_kube import FakeKube
    from tests.control.launcher.test_pod_launcher import _launcher

    kube = _launcher(tmp_path, FakeKube(), Channel(), egress=EgressPolicy.parse((), URL))
    assert isinstance(kube, KubernetesLauncher)
    # O despachante manda a URL inteira pelo canal (como os segredos do robô).
    wire = kube._options().wire()
    assert UpstreamProxy.parse(wire["proxy_saida"]).authorization == BASIC
    options = PodOptions.model_validate(wire)
    assert "Tr0ub4dor" not in repr(options)
    command = _worker_command(
        sys.executable, tmp_path, options, secrets=None, chromium=None, browser_args=[]
    )
    assert "--proxy-saida=http://proxy.interno:3128" in command
    assert not any("Tr0ub4dor" in part for part in command)
    assert _worker_env(tmp_path, options)[PROXY_CREDENTIALS_ENV] == f"usuario:{SENHA}"


def test_the_worker_takes_the_password_out_of_its_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_file = tmp_path / "job.json"
    job_file.write_text(_job().model_dump_json())
    monkeypatch.setenv(PROXY_CREDENTIALS_ENV, f"usuario:{SENHA}")
    seen: dict[str, Any] = {}

    class Stop(Exception):
        pass

    async def capture(args: Any, job: Any, secrets: Any, error: Any, egress: Any) -> Any:
        import os

        seen["egress"] = egress
        seen["env"] = os.environ.get(PROXY_CREDENTIALS_ENV)
        raise Stop

    monkeypatch.setattr(worker, "_main_async", capture)
    with pytest.raises(Stop):
        worker.main(
            [
                f"--job={job_file}",
                f"--result={tmp_path / 'r.json'}",
                f"--events={tmp_path / 'e.jsonl'}",
                f"--artifacts={tmp_path / 'a'}",
                "--guarda-de-saida",
                "--proxy-saida=http://proxy.interno:3128",
            ]
        )
    assert seen["egress"].upstream_proxy.authorization == BASIC
    # Fora do ambiente antes do Playwright: o Chromium herda o ambiente do driver.
    assert seen["env"] is None


def test_the_session_host_gets_the_password_in_its_config(tmp_path: Path) -> None:
    from rapa.control.sessions.launcher import KubernetesSessionLauncher
    from tests.control.launcher.fake_kube import FakeKube
    from tests.session.test_pod_sessions import _job as session_job
    from tests.session.test_pod_sessions import _launcher as session_launcher

    launcher = session_launcher(FakeKube(), tmp_path, egress=EgressPolicy.parse((), URL))
    assert isinstance(launcher, KubernetesSessionLauncher)
    config = launcher._config(session_job(), "t" * 43)  # vai num Secret montado
    parsed = HostConfig.model_validate(json.loads(json.dumps(config)))
    policy = parsed.policy()
    assert policy is not None and policy.upstream_proxy is not None
    assert policy.upstream_proxy.authorization == BASIC
    assert "Tr0ub4dor" not in repr(parsed)
