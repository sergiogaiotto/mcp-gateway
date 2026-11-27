"""Configuração das execuções e sessões em pods e da guarda da saída no serviço."""

import asyncio
import contextlib
import socket
from pathlib import Path
from typing import Any

import httpx2
import pytest

from rapa.control.channel import Channel
from rapa.control.db import Database
from rapa.control.launcher import KubernetesLauncher, LocalProcessLauncher
from rapa.control.services import PgPlatform
from rapa.control.sessions.launcher import KubernetesSessionLauncher, LocalSessionLauncher
from rapa.core.ids import new_id
from rapa.server import ServerSettings, _start_pod_launcher, build_sessions, create_server_app
from tests.control.launcher.fake_kube import FakeKube
from tests.control.launcher.test_launcher import RecordingIngestor
from tests.control.launcher.test_pod_launcher import _settings as pod_launcher_settings
from tests.control.launcher.tls import make_certificates
from tests.e2e.test_server import DB, _tokens

DISPATCHER = "postgresql://rapa_dispatcher:x@banco:5432/rapa"
PODS = {
    "RAPA_EXECUTOR": "kubernetes",
    "RAPA_K8S_IMAGEM_EXECUTOR": "registro.exemplo/rapa-executor:1.0",
    "RAPA_CANAL_ENDERECO": "http://{ip}:8081",
}


@pytest.fixture(autouse=True)
def _pod_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    """O IP deste "pod do despachante" (o que ``{ip}`` vira)."""
    monkeypatch.setattr("rapa.control.kube._own_address", lambda hostname=None: "10.1.2.3")


def _dev(tmp_path: Path) -> dict[str, str]:
    return {"RAPA_DATABASE_URL": DB, "RAPA_DEV": "1", "RAPA_TOKENS_DEV": str(_tokens(tmp_path))}


def test_the_defaults_keep_the_local_executor(tmp_path: Path) -> None:
    settings = ServerSettings.from_env(_dev(tmp_path))
    assert settings.executor == "local" and settings.sessions_runner == "local"
    assert settings.k8s_namespace == "rapa-execucoes" and settings.k8s_runtime_class == "gvisor"
    assert (settings.k8s_cpu, settings.k8s_memory, settings.channel_port) == ("1", "2Gi", 8081)
    # No desenvolvimento, sem redes nem proxy, o navegador local sai como sempre.
    assert not settings.egress_guard and settings.local_egress() is None


def test_the_guard_is_on_outside_dev_or_with_networks_or_a_proxy(tmp_path: Path) -> None:
    production = ServerSettings.from_env(
        {
            "RAPA_DATABASE_URL": DB,
            "RAPA_JWT_ISSUER": "https://sso.exemplo/realms/rapa",
            "RAPA_JWT_AUDIENCE": "rapa",
            "RAPA_JWKS_URL": "https://sso.exemplo/certs",
        }
    )
    assert production.egress_guard and production.local_egress() is not None
    networks = ServerSettings.from_env(
        {**_dev(tmp_path), "RAPA_REDES_INTERNAS_PERMITIDAS": " 10.20.0.0/16, 192.168.5.0/24 "}
    )
    assert networks.internal_networks == ("10.20.0.0/16", "192.168.5.0/24")
    policy = networks.local_egress()
    assert policy is not None and len(policy.allowed_networks) == 2
    proxy = ServerSettings.from_env(
        {**_dev(tmp_path), "RAPA_PROXY_SAIDA": "http://proxy.interno:3128"}
    )
    policy = proxy.local_egress()
    assert policy is not None and policy.upstream_proxy is not None
    assert policy.upstream_proxy.url == "http://proxy.interno:3128"


def test_a_browser_proxy_switch_the_guard_removes_is_warned_at_startup(tmp_path: Path) -> None:
    """Antes da guarda, o --proxy-server de RAPA_ARGS_NAVEGADOR valia para o executor; com
    ela, sai do navegador sem aviso nenhum, e toda navegação falha. A subida avisa, só com o
    nome do interruptor (o valor pode levar usuário e senha)."""
    args = (
        "--lang=pt-BR --proxy-server=http://ana:Tr0ub4dor@proxy.interno:3128 --proxy-bypass-list=*"
    )
    guarded = ServerSettings.from_env(
        {
            **_dev(tmp_path),
            "RAPA_REDES_INTERNAS_PERMITIDAS": "10.20.0.0/16",
            "RAPA_ARGS_NAVEGADOR": args,
        }
    )
    (warning,) = guarded.startup_warnings()
    assert "--proxy-bypass-list, --proxy-server" in warning and "RAPA_PROXY_SAIDA" in warning
    assert "Tr0ub4dor" not in warning and "proxy.interno" not in warning
    # Sem a guarda (desenvolvimento, sem redes nem proxy), o executor ainda os usa.
    plain = ServerSettings.from_env({**_dev(tmp_path), "RAPA_ARGS_NAVEGADOR": args})
    assert not plain.egress_guard and plain.startup_warnings() == []
    clean = ServerSettings.from_env(
        {**_dev(tmp_path), "RAPA_PROXY_SAIDA": "http://proxy.interno:3128"}
    )
    assert clean.startup_warnings() == []


def test_the_kubernetes_names_are_read(tmp_path: Path) -> None:
    settings = ServerSettings.from_env(
        {
            **_dev(tmp_path),
            **PODS,
            "RAPA_K8S_NAMESPACE_EXECUCOES": "robos",
            "RAPA_K8S_RUNTIME_CLASS": "",
            "RAPA_K8S_CPU": "1500m",
            "RAPA_K8S_MEMORIA": "3Gi",
            "RAPA_K8S_SA_EXECUTOR": "rapa-executor",
            "RAPA_K8S_PULL_SECRET": "registro",
            "RAPA_K8S_NODE_SELECTOR": '{"rapa.dev/navegadores": "sim"}',
            "RAPA_K8S_TOLERATIONS": '[{"key": "navegadores", "operator": "Exists"}]',
            "RAPA_CANAL_PORTA": "9091",
            "RAPA_SESSOES_EXECUTOR": "local",
        }
    )
    assert settings.executor == "kubernetes" and settings.sessions_runner == "local"
    pod = settings.pod_settings()
    assert pod.namespace == "robos" and pod.runtime_class is None  # vazia: sem RuntimeClass
    assert (pod.cpu, pod.memory) == ("1500m", "3Gi")
    assert pod.service_account == "rapa-executor" and pod.pull_secret == "registro"
    assert pod.node_selector == {"rapa.dev/navegadores": "sim"}
    assert pod.tolerations == ({"key": "navegadores", "operator": "Exists"},)
    assert settings.channel_port == 9091
    # A guarda sempre vale para as execuções em pods, mesmo no desenvolvimento.
    assert settings.egress_guard
    follows = ServerSettings.from_env({**_dev(tmp_path), **PODS})
    assert follows.sessions_runner == "kubernetes"


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"RAPA_EXECUTOR": "kubernetes"}, "RAPA_K8S_IMAGEM_EXECUTOR"),
        ({"RAPA_SESSOES_EXECUTOR": "kubernetes"}, "RAPA_K8S_IMAGEM_EXECUTOR"),
        ({"RAPA_EXECUTOR": "nuvem"}, "executor"),
        ({**PODS, "RAPA_K8S_NODE_SELECTOR": "{nao e json"}, "RAPA_K8S_NODE_SELECTOR"),
        ({**PODS, "RAPA_K8S_TOLERATIONS": '{"key": "a"}'}, "lista JSON"),
        ({**PODS, "RAPA_K8S_MEMORIA": "muita"}, "memória"),
        ({**PODS, "RAPA_K8S_NAMESPACE_EXECUCOES": "Robos"}, "namespace"),
        ({**PODS, "RAPA_CANAL_ENDERECO": "http://{ip}:8081/canal"}, "sem caminho"),
        # O nome de um Service: com duas réplicas, parte dos pods iria para a errada.
        (
            {**PODS, "RAPA_CANAL_ENDERECO": "http://rapa-despachante-canal.rapa.svc:8081"},
            "precisa levar",
        ),
        ({"RAPA_REDES_INTERNAS_PERMITIDAS": "127.0.0.0/8"}, "RAPA_REDES_INTERNAS_PERMITIDAS"),
        # O proxy com usuário e senha (de um Secret) vale; um erro nele não os repete.
        ({"RAPA_PROXY_SAIDA": "http://ana:Tr0ub4dor@proxy"}, "informe a porta"),
    ],
)
def test_bad_pod_configuration_is_refused(
    tmp_path: Path, env: dict[str, str], message: str
) -> None:
    with pytest.raises(ValueError, match=message) as caught:
        ServerSettings.from_env({**_dev(tmp_path), **env})
    assert "Tr0ub4dor" not in str(caught.value)


def _service_account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A conta de serviço montada do "pod" (o token e o CA do cluster)."""
    folder = tmp_path / "conta"
    make_certificates(folder)
    (folder / "token").write_text("token-da-conta")
    monkeypatch.setattr("rapa.control.kube.SERVICE_ACCOUNT_DIR", folder)
    return folder


def test_the_dispatcher_picks_the_launcher_by_the_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = {"RAPA_DATABASE_URL": DB, "RAPA_DISPATCHER_DATABASE_URL": DISPATCHER, "RAPA_HTTP": "0"}
    local = create_server_app(ServerSettings.from_env(base)).state.dispatcher
    assert isinstance(local.launch, LocalProcessLauncher)
    # Fora do desenvolvimento, o worker local sai pela guarda.
    assert local.launch.settings.egress is not None
    # Sem estar num pod do cluster, o modo kubernetes não sobe.
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    with pytest.raises(ValueError, match="KUBERNETES_SERVICE_HOST"):
        create_server_app(ServerSettings.from_env({**base, **PODS}))
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
    # Sem o CA do cluster montado, também não: nunca os CAs do sistema no lugar dele.
    monkeypatch.setattr("rapa.control.kube.SERVICE_ACCOUNT_DIR", tmp_path / "sem-conta")
    with pytest.raises(ValueError, match="CA do cluster"):
        create_server_app(ServerSettings.from_env({**base, **PODS}))
    _service_account(tmp_path, monkeypatch)
    files = tmp_path / "arquivos"
    pods = create_server_app(
        ServerSettings.from_env(
            {
                **base,
                **PODS,
                "RAPA_REDES_INTERNAS_PERMITIDAS": "10.20.0.0/16",
                "RAPA_PASTA_ARQUIVOS": str(files),
            }
        )
    ).state.dispatcher
    launcher = pods.launch
    assert isinstance(launcher, KubernetesLauncher)
    # {ip} virou o IP deste pod: cada réplica recebe os pods que ela lançou.
    assert launcher.settings.channel_address == "http://10.1.2.3:8081"
    # Os arquivos de entrada (os passos de envio) também valem no modo kubernetes.
    assert launcher.settings.files_dir == files
    assert launcher.settings.pod.image == "registro.exemplo/rapa-executor:1.0"
    assert [str(n) for n in launcher.settings.egress.allowed_networks] == ["10.20.0.0/16"]
    assert launcher.attempts is pods.queue  # a varredura pergunta ao banco do despachante
    assert launcher.api.config.base_url == "https://10.96.0.1:443"


def test_the_sessions_pick_the_launcher_by_the_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database.from_url(DB)
    platform = PgPlatform(db)
    _, local = build_sessions(ServerSettings.from_env(_dev(tmp_path)), db, platform)
    assert isinstance(local, LocalSessionLauncher) and local.settings.egress is None
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")
    _service_account(tmp_path, monkeypatch)
    _, pods = build_sessions(
        ServerSettings.from_env(
            {**_dev(tmp_path), **PODS, "RAPA_PROXY_SAIDA": "http://p.interno:3128"}
        ),
        db,
        platform,
    )
    assert isinstance(pods, KubernetesSessionLauncher)
    assert pods.settings.pod.image == "registro.exemplo/rapa-executor:1.0"
    proxy = pods.settings.egress.upstream_proxy
    assert proxy is not None and proxy.url == "http://p.interno:3128"


async def test_the_channel_and_the_sweeper_start_with_the_service(tmp_path: Path) -> None:
    fake = FakeKube()
    fake.add(
        "pods",
        {
            "metadata": {
                "name": "orfao",
                "labels": {
                    "app.kubernetes.io/name": "rapa-executor",
                    "rapa.dev/execucao": str(new_id()),
                    "rapa.dev/tentativa": "1",
                },
            }
        },
    )

    class NoneLive:
        async def live_attempts(self, attempts: Any) -> set[Any]:
            return set()

    launcher = KubernetesLauncher(
        RecordingIngestor(),
        pod_launcher_settings(tmp_path),
        api=fake.api(tmp_path),
        channel=Channel(),
        attempts=NoneLive(),
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    async with contextlib.AsyncExitStack() as stack:
        await _start_pod_launcher(stack, launcher, port)
        async with httpx2.AsyncClient(trust_env=False) as http:
            response = await http.get(f"http://127.0.0.1:{port}/canal/{new_id()}/1/trabalho")
        assert response.status_code == 401
        # A varredura roda na subida: o pod órfão sai.
        for _ in range(100):
            if not fake.objects:
                break
            await asyncio.sleep(0.02)
        assert fake.objects == {}
    # No encerramento, o canal fecha.
    with pytest.raises(httpx2.ConnectError):
        async with httpx2.AsyncClient(trust_env=False) as http:
            await http.get(f"http://127.0.0.1:{port}/canal/{new_id()}/1/trabalho")


def test_the_dispatcher_ports_never_cross(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """O canal tem o servidor dele: a porta do serviço (a da saúde do despachante) e a das
    métricas nunca são a do canal, ou o processo não sobe (com a mensagem, sem traceback)."""
    from rapa.server import _check_ports, main

    env = {**_dev(tmp_path), **PODS, "RAPA_DISPATCHER_DATABASE_URL": DISPATCHER}
    settings = ServerSettings.from_env(env)
    _check_ports(settings, 8080)  # a saúde do despachante, como no chart
    with pytest.raises(ValueError, match="RAPA_CANAL_PORTA"):
        _check_ports(settings, 8081)
    metrics = ServerSettings.from_env({**env, "RAPA_METRICAS_PORTA": "8081"})
    with pytest.raises(ValueError, match="canal"):
        _check_ports(metrics, 8080)
    # Sem despachante (a web e a api), a porta do canal não abre: nada a conferir.
    _check_ports(ServerSettings.from_env({**_dev(tmp_path), **PODS}), 8081)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert main(["--port", "8081"]) == 2
    err = capsys.readouterr().err
    assert "configuração inválida" in err and "RAPA_CANAL_PORTA" in err
    assert "Traceback" not in err
