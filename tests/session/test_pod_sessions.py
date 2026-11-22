"""Sessões de gravação em pods: o manifesto e o aperto de mão contra a API falsa, e o
hospedeiro no modo arquivo (a configuração no Secret montado, em vez da entrada padrão)."""

import asyncio
import json
import os
import signal
import socket
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from rapa.control.kube import PodSettings
from rapa.control.sessions.launcher import (
    KubernetesSessionLauncher,
    KubernetesSessionSettings,
    SessionJob,
    SessionNotStarted,
)
from rapa.core.ids import new_id
from rapa.fixtures.server import HOST_RESOLVER_ARG, RunningPortal
from rapa.session.egress import EgressPolicy
from rapa.session.host import HostConfig, _with_image_args
from tests.control.launcher.fake_kube import FakeKube
from tests.session.support import LiveClient

IMAGE = "registro.exemplo/rapa-executor:1.0"


def _launcher(fake: FakeKube, tmp_path: Path, **changes: Any) -> KubernetesSessionLauncher:
    values: dict[str, Any] = {
        "pod": PodSettings(image=IMAGE),
        "browser_args": ("--lang=pt-BR",),
        "egress": EgressPolicy.parse("10.20.0.0/16", "http://proxy.interno:3128"),
        "start_timeout_s": 5.0,
        "status_interval_s": 0.05,
        "kill_grace_s": 3.0,
        **changes,
    }
    return KubernetesSessionLauncher(KubernetesSessionSettings(**values), api=fake.api(tmp_path))


def _job(**changes: Any) -> SessionJob:
    values: dict[str, Any] = {
        "session_id": new_id(),
        "company_id": new_id(),
        "start_url": "https://portal.exemplo/",
        **changes,
    }
    return SessionJob(**values)


def _ready_soon(fake: FakeKube, ip: str = "10.9.8.7") -> list[dict[str, Any]]:
    created: list[dict[str, Any]] = []

    async def ready(name: str) -> None:
        await asyncio.sleep(0.2)
        fake.set_status(
            name,
            {"phase": "Running", "podIP": ip, "conditions": [{"type": "Ready", "status": "True"}]},
        )

    def on_pod(pod: dict[str, Any]) -> None:
        created.append(json.loads(json.dumps(pod)))
        asyncio.get_running_loop().create_task(ready(pod["metadata"]["name"]))

    fake.on_pod = on_pod
    return created


async def test_the_session_pod_and_the_handshake(tmp_path: Path) -> None:
    fake = FakeKube()
    created = _ready_soon(fake)
    launcher = _launcher(fake, tmp_path)
    job = _job()
    endpoint = await launcher.start(job)

    # O aperto de mão é o pod pronto (prontidão TCP) mais o IP dele.
    assert endpoint.url == "ws://10.9.8.7:8765/"
    assert endpoint.token not in repr(endpoint) and endpoint.token not in repr(launcher)
    assert await launcher.is_alive(job.session_id)
    (pod,) = created
    name = f"rapa-sessao-{job.session_id}"
    meta, spec = pod["metadata"], pod["spec"]
    assert meta["name"] == name
    assert meta["labels"] == {
        "app.kubernetes.io/name": "rapa-sessao",
        "app.kubernetes.io/part-of": "rapa",
        "rapa.dev/sessao": str(job.session_id),
        "rapa.dev/empresa": str(job.company_id),
    }
    (container,) = spec["containers"]
    assert container["image"] == IMAGE
    assert container["args"] == [
        "python",
        "-m",
        "rapa.session.host",
        "--config",
        "/var/run/rapa/sessao/config.json",
    ]
    assert container["ports"] == [{"name": "canal", "containerPort": 8765, "protocol": "TCP"}]
    assert container["readinessProbe"]["tcpSocket"] == {"port": 8765}
    # As mesmas regras de segurança dos pods de execução.
    assert spec["automountServiceAccountToken"] is False and spec["restartPolicy"] == "Never"
    assert spec["enableServiceLinks"] is False and spec["runtimeClassName"] == "gvisor"
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert container["securityContext"]["runAsUser"] == 10001
    # Os temporários do navegador (os downloads da gravação) vão para o disco do trabalho,
    # nunca para o /tmp em memória, que conta no limite de memória do pod.
    assert {"name": "TMPDIR", "value": "/trabalho"} in container["env"]
    mounts = {m["name"]: m["mountPath"] for m in container["volumeMounts"]}
    volumes = {v["name"]: v for v in spec["volumes"]}
    assert mounts["trabalho"] == "/trabalho" and "medium" not in volumes["trabalho"]["emptyDir"]
    assert spec["activeDeadlineSeconds"] == 3600 + 30
    assert meta["ownerReferences"][0]["kind"] == "Secret"
    # A configuração vai no Secret montado, com o token, e o hospedeiro a aceita.
    config = json.loads(fake.secret_data(name)["config.json"])
    assert config["token"] == endpoint.token
    assert config["listen_host"] == "0.0.0.0" and config["listen_port"] == 8765
    assert config["profile_parent"] == "/trabalho"
    assert config["egress_policy"] is True
    assert config["allowed_networks"] == ["10.20.0.0/16"]
    assert config["upstream_proxy"] == "http://proxy.interno:3128"
    parsed = HostConfig.model_validate(config)
    policy = parsed.policy()
    assert policy is not None and [str(n) for n in policy.allowed_networks] == ["10.20.0.0/16"]
    # O vigia e o encerramento: o pod e o Secret saem.
    await launcher.terminate(job.session_id)
    assert not await launcher.is_alive(job.session_id)
    assert fake.objects == {}
    deletes = {plural: options for plural, n, options in fake.deleted if n == name}
    assert deletes["pods"]["gracePeriodSeconds"] == 3


@pytest.mark.parametrize("reason", ["ImagePullBackOff", "CreateContainerConfigError"])
async def test_a_session_pod_that_cannot_start_is_refused(tmp_path: Path, reason: str) -> None:
    fake = FakeKube()

    def on_pod(pod: dict[str, Any]) -> None:
        pod["status"] = {
            "phase": "Pending",
            "containerStatuses": [{"state": {"waiting": {"reason": reason}}}],
        }

    fake.on_pod = on_pod
    launcher = _launcher(fake, tmp_path)
    job = _job()
    with pytest.raises(SessionNotStarted):
        await launcher.start(job)
    assert fake.objects == {} and not await launcher.is_alive(job.session_id)


async def test_a_session_pod_that_never_gets_ready_times_out(tmp_path: Path) -> None:
    fake = FakeKube()
    launcher = _launcher(fake, tmp_path, start_timeout_s=0.3)
    with pytest.raises(SessionNotStarted, match="a tempo"):
        await launcher.start(_job())
    assert fake.objects == {}
    fake.refuse["secrets"] = 403
    with pytest.raises(SessionNotStarted):
        await launcher.start(_job())
    assert fake.objects == {}


async def test_stop_waits_for_the_host_to_leave_and_cleans_up(tmp_path: Path) -> None:
    fake = FakeKube()
    _ready_soon(fake)
    launcher = _launcher(fake, tmp_path)
    job = _job()
    await launcher.start(job)
    name = f"rapa-sessao-{job.session_id}"

    async def leave() -> None:
        await asyncio.sleep(0.3)
        fake.set_status(name, {"phase": "Succeeded"})

    leaving = asyncio.create_task(leave())
    loop = asyncio.get_running_loop()
    started = loop.time()
    await launcher.stop(job.session_id)
    await leaving
    assert loop.time() - started < 5
    assert fake.objects == {} and launcher.running == 0


async def test_the_watcher_ends_the_pod_after_the_maximum_duration(tmp_path: Path) -> None:
    fake = FakeKube()
    _ready_soon(fake)
    launcher = _launcher(fake, tmp_path, deadline_grace_s=0.2)
    job = _job(max_duration_s=0.4, idle_timeout_s=0.4)
    await launcher.start(job)
    for _ in range(100):
        if not await launcher.is_alive(job.session_id):
            break
        await asyncio.sleep(0.05)
    assert not await launcher.is_alive(job.session_id)
    assert fake.objects == {}
    await launcher.aclose()


async def test_the_sweep_removes_finished_session_pods_of_other_replicas(tmp_path: Path) -> None:
    fake = FakeKube()
    labels = {"app.kubernetes.io/name": "rapa-sessao"}
    fake.add(
        "pods", {"metadata": {"name": "acabou", "labels": labels}, "status": {"phase": "Failed"}}
    )
    fake.add("secrets", {"metadata": {"name": "acabou", "labels": labels}})
    fake.add(
        "pods", {"metadata": {"name": "rodando", "labels": labels}, "status": {"phase": "Running"}}
    )
    fake.add("secrets", {"metadata": {"name": "rodando", "labels": labels}})
    fake.add("secrets", {"metadata": {"name": "sem-pod", "labels": labels}})
    launcher = _launcher(fake, tmp_path)
    assert await launcher.sweep() == 2
    assert sorted((plural, name) for _, plural, name in fake.objects) == [
        ("pods", "rodando"),
        ("secrets", "rodando"),
    ]


async def test_a_sweep_between_the_secret_and_the_pod_keeps_the_session(
    tmp_path: Path,
) -> None:
    fake = FakeKube()
    _ready_soon(fake)
    launcher = _launcher(fake, tmp_path)
    other_replica = _launcher(fake, tmp_path)
    create = launcher.api.create
    swept: list[int] = []

    async def create_then_sweep(namespace: str, plural: str, body: Any) -> dict[str, Any]:
        if plural == "pods":
            # O Secret já existe e o pod ainda não: as varreduras rodam bem aqui.
            swept.append(await other_replica.sweep())
            swept.append(await launcher.sweep())
        return await create(namespace, plural, body)

    launcher.api.create = create_then_sweep  # type: ignore[method-assign]
    job = _job()
    endpoint = await launcher.start(job)
    assert swept == [0, 0]
    name = f"rapa-sessao-{job.session_id}"
    # O Secret (dono do pod) ficou: o coletor não leva o pod novo junto.
    assert ("rapa-execucoes", "secrets", name) in fake.objects
    assert ("rapa-execucoes", "pods", name) in fake.objects
    assert endpoint.url == "ws://10.9.8.7:8765/" and await launcher.is_alive(job.session_id)
    await launcher.aclose()


async def test_the_sweep_removes_only_old_secrets_without_a_pod(tmp_path: Path) -> None:
    fake = FakeKube()
    labels = {"app.kubernetes.io/name": "rapa-sessao"}
    now = datetime.now(UTC)
    for name, age in (("recente", 10), ("antigo", 3600)):
        created = (now - timedelta(seconds=age)).strftime("%Y-%m-%dT%H:%M:%SZ")
        fake.add(
            "secrets", {"metadata": {"name": name, "labels": labels, "creationTimestamp": created}}
        )
    launcher = _launcher(fake, tmp_path)
    assert await launcher.sweep() == 1
    # O recente pode ser de uma sessão subindo em outra réplica: o pod ainda vem.
    assert [name for _, _, name in fake.objects] == ["recente"]


def test_the_image_browser_args_come_first() -> None:
    config = HostConfig(
        token="t" * 43, start_url="https://portal.exemplo/", browser_args=("--lang=pt-BR",)
    )
    changed = _with_image_args(config, {"RAPA_ARGS_NAVEGADOR": "--no-sandbox --x='a b'"})
    assert changed.browser_args == ("--no-sandbox", "--x=a b", "--lang=pt-BR")
    assert _with_image_args(config, {}) is config


def test_networks_and_proxy_need_the_policy() -> None:
    base = {"token": "t" * 43, "start_url": "https://portal.exemplo/"}
    with pytest.raises(ValueError, match="só valem com egress_policy"):
        HostConfig.model_validate({**base, "allowed_networks": ["10.0.0.0/8"]})
    with pytest.raises(ValueError, match="sempre recusa"):
        HostConfig.model_validate(
            {**base, "egress_policy": True, "allowed_networks": ["127.0.0.0/8"]}
        )
    assert HostConfig.model_validate(base).policy() is None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.browser
async def test_the_host_reads_its_configuration_from_a_file(
    chromium_path: str, portal_v1: RunningPortal, tmp_path: Path
) -> None:
    port = _free_port()
    token = "s" * 43
    config = tmp_path / "sessao" / "config.json"
    config.parent.mkdir()
    config.write_text(
        json.dumps(
            {
                "token": token,
                "start_url": f"{portal_v1.base_url}/",
                "listen_host": "127.0.0.1",
                "listen_port": port,
                "chromium": chromium_path,
                "browser_args": [HOST_RESOLVER_ARG],
                "profile_parent": str(tmp_path),
                "egress_policy": True,
            }
        )
    )
    env = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "PLAYWRIGHT_BROWSERS_PATH"}}
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "rapa.session.host",
        "--config",
        str(config),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=env,
    )
    try:
        assert process.stdout is not None
        line = await asyncio.wait_for(process.stdout.readline(), timeout=60)
        assert json.loads(line) == {"pronto": True, "porta": port}
        # A prontidão do pod é a porta aceitando conexões.
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.close()
        with pytest.raises(InvalidStatus) as caught:
            await connect(
                f"ws://127.0.0.1:{port}/", additional_headers={"Authorization": "Bearer x"}
            )
        assert caught.value.response.status_code == 401
        ws = await connect(
            f"ws://127.0.0.1:{port}/",
            additional_headers={"Authorization": f"Bearer {token}"},
            max_size=None,
        )
        client = LiveClient(ws)
        try:
            await client.event("pronta")
        finally:
            await client.close()
    finally:
        if process.returncode is None:
            process.send_signal(signal.SIGTERM)
        assert await asyncio.wait_for(process.wait(), timeout=30) == 0


async def test_an_unreadable_configuration_file_stops_the_host(tmp_path: Path) -> None:
    for content in (None, "não é json", json.dumps({"token": "curto", "start_url": "x"})):
        path = tmp_path / "config.json"
        if content is not None:
            path.write_text(content)
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "rapa.session.host",
            "--config",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(process.communicate(), timeout=30)
        assert process.returncode != 0 and out == b""
        assert b"curto" not in err  # a configuração (com o token) nunca é repetida


async def test_the_host_gives_its_policy_to_the_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rapa.session import host as host_module

    captured: dict[str, Any] = {}
    real = host_module.EgressGuard.from_browser_args

    def spy(args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return real(args, **kwargs)

    async def no_browser(self: Any, ready: Any, port: int, profile: Any, ended: Any) -> Any:
        return host_module.EndReason.SIGNAL

    monkeypatch.setattr(host_module.EgressGuard, "from_browser_args", staticmethod(spy))
    monkeypatch.setattr(host_module.SessionHost, "_run_browser", no_browser)
    config = HostConfig(
        token="t" * 43,
        start_url="https://portal.exemplo/",
        profile_parent=str(tmp_path),
        egress_policy=True,
        allowed_networks=("10.20.0.0/16",),
        upstream_proxy="http://proxy.interno:3128",
    )
    await host_module.SessionHost(config).run(lambda _port: None)
    policy = captured["policy"]
    assert [str(n) for n in policy.allowed_networks] == ["10.20.0.0/16"]
    assert policy.upstream_proxy.url == "http://proxy.interno:3128"
    captured.clear()
    plain = HostConfig(
        token="t" * 43, start_url="https://portal.exemplo/", profile_parent=str(tmp_path)
    )
    await host_module.SessionHost(plain).run(lambda _port: None)
    assert captured["policy"] is None


_RECORDING_HOST = """#!{python}
import json, sys, time
line = sys.stdin.readline()
open({out!r}, "w").write(line)
print(json.dumps({{"pronto": True, "porta": 4321}}), flush=True)
time.sleep(30)
"""


async def test_the_local_launcher_hands_the_policy_to_the_host(tmp_path: Path) -> None:
    from rapa.control.sessions import LocalSessionLauncher, LocalSessionSettings

    out = tmp_path / "config.json"
    script = tmp_path / "hospedeiro.py"
    script.write_text(_RECORDING_HOST.format(python=sys.executable, out=str(out)))
    script.chmod(0o755)
    launcher = LocalSessionLauncher(
        LocalSessionSettings(
            python=str(script),
            kill_grace_s=1,
            profile_dir=tmp_path / "sessoes",
            egress=EgressPolicy.parse("10.20.0.0/16", "http://proxy.interno:3128"),
        )
    )
    job = _job()
    try:
        await launcher.start(job)
        config = json.loads(out.read_text())
    finally:
        await launcher.aclose()
    assert config["egress_policy"] is True
    assert config["allowed_networks"] == ["10.20.0.0/16"]
    assert config["upstream_proxy"] == "http://proxy.interno:3128"
    HostConfig.model_validate(config)  # o hospedeiro aceita
