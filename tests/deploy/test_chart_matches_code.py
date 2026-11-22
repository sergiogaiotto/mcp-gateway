"""O chart e o código do RaPA concordam: o que o chart põe no ambiente de cada componente
passa pelas conferências da partida do servidor, e os pods que o lançador de verdade cria
(execução e sessão) são os que o RBAC, a ValidatingAdmissionPolicy e as NetworkPolicies do
chart esperam (o namespace, os rótulos, a conta de serviço, a RuntimeClass, a imagem, os
argumentos, os volumes e as portas).

Os segredos que o chart só referencia (``valueFrom``) entram aqui com valores de mentira, e
o ``$(VAR)`` é expandido como o Kubernetes faz.
"""

import ast
import asyncio
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from rapa.control.channel import Channel
from rapa.control.kube import channel_address
from rapa.control.launcher import KubernetesLauncher, KubernetesLauncherSettings
from rapa.control.sessions.launcher import (
    KubernetesSessionLauncher,
    KubernetesSessionSettings,
    SessionJob,
)
from rapa.core.contracts import RunnerJob
from rapa.core.ids import new_id
from rapa.fixtures.robots import emitir_protocolo
from rapa.server import ServerSettings, _check_ports
from tests.control.launcher.fake_kube import FakeKube
from tests.control.launcher.test_launcher import INPUTS, RecordingIngestor
from tests.deploy.support import REPO, Render, container_env, deployment_pod, template

#: Valores de mentira para o que o chart lê de Secrets (nunca vão para o render).
_FROM_SECRETS = {
    "RAPA_CHAVE_WEBHOOKS": "c" * 40,
    "RAPA_CHAVE_ARTEFATOS": "a" * 40,
    "RAPA_GLITCHTIP_DSN": "https://chave@glitchtip.exemplo.test/1",
    "RAPA_PROXY_SAIDA": "http://proxy.interno:3128",
}

_VARIANTS = {"producao": ("values-producao-exemplo.yaml",), "kind": ("values-kind.yaml",)}


def _render(variant: str) -> Render:
    return template(*_VARIANTS[variant])


def _env(render: Render, deployment: str) -> tuple[dict[str, str], list[str]]:
    """O ambiente do contêiner como o processo o vê, e os argumentos dele."""
    pod = deployment_pod(render, deployment)
    raw = container_env(pod)
    env: dict[str, str] = {}
    for name, value in raw.items():
        if isinstance(value, str):
            env[name] = re.sub(r"\$\((\w+)\)", lambda m: env.get(m[1], m[0]), value)
        elif name in _FROM_SECRETS:
            env[name] = _FROM_SECRETS[name]
        else:
            env[name] = "valor-de-um-secret"
    return env, list(pod["containers"][0]["args"])


def _port(args: list[str]) -> int:
    return int(args[args.index("--port") + 1])


@pytest.mark.parametrize("variant", sorted(_VARIANTS))
@pytest.mark.parametrize("component", ["web", "api", "despachante"])
def test_each_component_starts_with_the_chart_environment(variant: str, component: str) -> None:
    env, args = _env(_render(variant), f"rapa-{component}")
    settings = ServerSettings.from_env(env)
    _check_ports(settings, _port(args))
    if component == "despachante":
        assert settings.executor == "kubernetes" and not settings.http
        assert settings.dispatcher_database_url
        # O endereço do canal leva {ip}: cada pod fala com a réplica que o lançou.
        assert (
            channel_address(
                settings.channel_address, settings.channel_port, resolve_ip=lambda: "10.0.0.7"
            )
            == f"http://10.0.0.7:{settings.channel_port}"
        )
        assert (
            channel_address(
                settings.channel_address, settings.channel_port, resolve_ip=lambda: "fd00::7"
            )
            == f"http://[fd00::7]:{settings.channel_port}"
        )
        assert _port(args) != settings.channel_port
    if component == "web":
        assert settings.sessions_runner == "kubernetes"
    if component in ("web", "despachante"):
        settings.pod_settings()  # a imagem, o namespace e os limites no formato do Kubernetes


def test_a_service_name_as_the_channel_address_is_refused() -> None:
    """O exemplo do contrato (um Service) não sobe: com duas réplicas, parte dos pods cairia
    na réplica errada."""
    env, _ = _env(_render("producao"), "rapa-despachante")
    env["RAPA_CANAL_ENDERECO"] = "http://rapa-despachante-canal.rapa.svc:8081"
    with pytest.raises(ValueError, match="ip"):
        ServerSettings.from_env(env)


def _run_job() -> RunnerJob:
    return RunnerJob(
        run_id=new_id(),
        company_id=new_id(),
        attempt=1,
        manifest=emitir_protocolo("http://portal-v1.rapa.test:8080", "portal-v1.rapa.test"),
        inputs=INPUTS,
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    )


async def _run_pod(settings: ServerSettings, tmp_path: Path) -> dict[str, Any]:
    """O pod que o lançador de execuções de verdade cria com a configuração do chart."""
    await asyncio.to_thread(tmp_path.mkdir, parents=True, exist_ok=True)
    fake, seen = FakeKube(), {}

    def on_pod(pod: dict[str, Any]) -> None:
        seen["pod"] = json.loads(json.dumps(pod))
        pod["status"] = {
            "phase": "Pending",
            "containerStatuses": [
                {"name": "executor", "state": {"waiting": {"reason": "ImagePullBackOff"}}}
            ],
        }

    fake.on_pod = on_pod
    launcher = KubernetesLauncher(
        RecordingIngestor(),
        KubernetesLauncherSettings(
            work_dir=tmp_path / "trabalho",
            artifacts_dir=tmp_path / "artefatos",
            pod=settings.pod_settings(),
            channel_address=channel_address(
                settings.channel_address, settings.channel_port, resolve_ip=lambda: "10.0.0.7"
            ),
            browser_args=settings.browser_args,
            egress=settings.egress_policy(),
            poll_s=0.02,
            status_interval_s=0.05,
        ),
        api=fake.api(tmp_path),
        channel=Channel(),
    )
    await launcher(_run_job())
    pod: dict[str, Any] = seen["pod"]
    return pod


async def _session_pod(settings: ServerSettings, tmp_path: Path) -> tuple[dict[str, Any], str]:
    """O pod que o lançador de sessões de verdade cria com a configuração do chart."""
    await asyncio.to_thread(tmp_path.mkdir, parents=True, exist_ok=True)
    fake, created = FakeKube(), []

    async def ready(name: str) -> None:
        await asyncio.sleep(0.05)
        fake.set_status(
            name,
            {
                "phase": "Running",
                "podIP": "10.9.8.7",
                "conditions": [{"type": "Ready", "status": "True"}],
            },
        )

    def on_pod(pod: dict[str, Any]) -> None:
        created.append(json.loads(json.dumps(pod)))
        asyncio.get_running_loop().create_task(ready(pod["metadata"]["name"]))

    fake.on_pod = on_pod
    launcher = KubernetesSessionLauncher(
        KubernetesSessionSettings(
            pod=settings.pod_settings(),
            browser_args=settings.browser_args,
            egress=settings.egress_policy(),
            start_timeout_s=5.0,
            status_interval_s=0.05,
        ),
        api=fake.api(tmp_path),
    )
    job = SessionJob(session_id=new_id(), company_id=new_id(), start_url="https://portal.test/")
    try:
        endpoint = await launcher.start(job)
    finally:
        await launcher.aclose()
    return created[0], endpoint.url


async def test_the_proxy_password_from_the_secret_starts_and_stays_off_the_pods(
    tmp_path: Path,
) -> None:
    """O proxy de saída com usuário e senha, como o chart o oferece (a URL inteira num Secret,
    executions.egress.proxy.existingSecret): a web e o despachante sobem com ele, e a senha
    não vai no manifesto de nenhum pod (o executor a recebe pelo canal; a sessão, no Secret
    dela)."""
    render = template(
        "values-producao-exemplo.yaml",
        sets={
            "executions.egress.proxy.url": "",
            "executions.egress.proxy.existingSecret": "rapa-proxy",
        },
    )
    secret_url = "http://robos:S3nh%40-do-proxy@proxy.interno:3128"
    settings: dict[str, ServerSettings] = {}
    for component in ("web", "despachante"):
        env, args = _env(render, f"rapa-{component}")
        env["RAPA_PROXY_SAIDA"] = secret_url  # o valor do Secret rapa-proxy
        settings[component] = ServerSettings.from_env(env)
        _check_ports(settings[component], _port(args))
        proxy = settings[component].egress_policy().upstream_proxy
        assert proxy is not None and proxy.url == "http://proxy.interno:3128"
        assert proxy.authorization is not None
        assert "S3nh" not in repr(settings[component])
    run = await _run_pod(settings["despachante"], tmp_path / "execucao")
    session, _ = await _session_pod(settings["web"], tmp_path / "sessao")
    for pod in (run, session):
        assert "S3nh" not in json.dumps(pod)


def _admitted(policy: Mapping[str, Any], pod: Mapping[str, Any], app: str, creator: str) -> None:
    """As regras da ValidatingAdmissionPolicy do chart, conferidas no pod de verdade.

    O CEL da política é lido do render (a imagem, a conta e a RuntimeClass que ele fixa, o
    rótulo de cada conta, os argumentos, as variáveis e as pastas), e cada regra é refeita
    aqui: um pod do lançador que o API server recusaria (criado por ``creator``) falha.
    """
    variables = {v["name"]: v["expression"] for v in policy["spec"]["variables"]}
    rules = " ".join(" ".join(v["expression"].split()) for v in policy["spec"]["validations"])
    spec = pod["spec"]
    (container,) = spec["containers"]
    labels = pod["metadata"]["labels"]
    assert labels["app.kubernetes.io/name"] == app
    # O rótulo é o da conta que cria o pod (o despachante, as execuções; a web, as sessões).
    choice = r"(?:request\.userInfo\.username|variables\.app) == '([^']+)' \? '([^']+)' : '([^']+)'"
    expected = re.fullmatch(choice, variables["expected"])
    assert expected and (expected[2] if creator == expected[1] else expected[3]) == app
    account = re.search(r"serviceAccountName == '([^']+)'", rules)
    assert account and spec.get("serviceAccountName") == account[1]
    assert spec.get("automountServiceAccountToken") is False
    runtime = re.search(r"runtimeClassName == '([^']+)'", rules)
    assert spec.get("runtimeClassName") == (runtime[1] if runtime else None)
    # O lançador desta versão usa a primeira (as outras são as da versão anterior).
    image = re.search(r"c\.image in \['([^']+)'", rules)
    assert image and container["image"] == image[1]
    assert not spec.get("initContainers") and not spec.get("ephemeralContainers")
    assert "command" not in container and "workingDir" not in container
    args = re.search(
        r"c\.args == \(variables\.app == '([^']+)' \? (\[[^\]]*\]) : (\[[^\]]*\])\)", rules
    )
    assert args
    assert container["args"] == ast.literal_eval(args[2] if app == args[1] else args[3])
    # Nenhum gancho nem sonda que rode um comando; a prontidão, só tcpSocket sem host.
    for field in ("lifecycle", "livenessProbe", "startupProbe"):
        assert field not in container
    readiness = container.get("readinessProbe")
    if readiness is not None:
        assert {"exec", "httpGet", "grpc", "tcpSocket"} & set(readiness) == {"tcpSocket"}
        assert "host" not in readiness["tcpSocket"]
    assert "envFrom" not in container
    allowed = re.search(r"v\.name in (\[[^\]]*\])", rules)
    assert allowed
    for variable in container.get("env", []):
        assert variable["name"] in ast.literal_eval(allowed[1]), variable["name"]
        assert "valueFrom" not in variable
    for volume in spec["volumes"]:
        assert "secret" in volume or "emptyDir" in volume, volume
    # O Secret só na pasta dele, os emptyDir só nas outras, sem subPath.
    secret_dir = re.fullmatch(choice, variables["secretDir"])
    assert secret_dir
    directory = secret_dir[2] if app == secret_dir[1] else secret_dir[3]
    others = re.search(r"m\.mountPath in \[variables\.secretDir, ([^\]]*)\]", rules)
    assert others
    paths = [directory, *ast.literal_eval(f"[{others[1]}]")]
    secret_volumes = {v["name"] for v in spec["volumes"] if "secret" in v}
    for mount in container["volumeMounts"]:
        assert "subPath" not in mount and "subPathExpr" not in mount
        assert mount["mountPath"] in paths, mount
        assert (mount["name"] in secret_volumes) == (mount["mountPath"] == directory), mount
    # E a Pod Security "restricted" do namespace, que a política também confere.
    for field in ("hostNetwork", "hostPID", "hostIPC"):
        assert f"!(has(object.spec.{field}) && object.spec.{field})" in rules
        assert not spec.get(field)
    assert spec["securityContext"]["runAsNonRoot"] is True
    assert spec["securityContext"].get("runAsUser") != 0
    security = container["securityContext"]
    assert security["allowPrivilegeEscalation"] is False
    assert security.get("privileged") is not True and security.get("runAsUser") != 0
    assert security["capabilities"] == {"drop": ["ALL"]}
    assert "c.securityContext.capabilities.drop == ['ALL']" in rules
    assert security["runAsNonRoot"] is True
    assert not any(port.get("hostPort") for port in container.get("ports", []))
    assert spec["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}


def _policy(render: Render, namespace: str, name: str) -> dict[str, Any]:
    return render.get("NetworkPolicy", name, namespace)


@pytest.mark.parametrize("variant", sorted(_VARIANTS))
async def test_the_launchers_pods_are_what_the_chart_allows(variant: str, tmp_path: Path) -> None:
    render = _render(variant)
    values = {
        "namespace": "rapa-execucoes",
        "run": "rapa-executor",
        "session": "rapa-sessao",
    }
    dispatcher, _ = _env(render, "rapa-despachante")
    web, _ = _env(render, "rapa-web")
    run = await _run_pod(ServerSettings.from_env(dispatcher), tmp_path / "execucao")
    session, url = await _session_pod(ServerSettings.from_env(web), tmp_path / "sessao")
    (admission,) = render.of_kind("ValidatingAdmissionPolicy")
    accounts = {
        values["run"]: "system:serviceaccount:rapa:rapa-despachante",
        values["session"]: "system:serviceaccount:rapa:rapa-web",
    }
    for pod, app in ((run, values["run"]), (session, values["session"])):
        assert pod["metadata"]["namespace"] == values["namespace"]
        _admitted(admission, pod, app, accounts[app])
        # A conta dos pods existe no namespace das execuções, sem token.
        account = render.get(
            "ServiceAccount", pod["spec"]["serviceAccountName"], values["namespace"]
        )
        assert account["automountServiceAccountToken"] is False
        # A política de rede do namespace escolhe o pod pelo rótulo.
        selected = _policy(render, values["namespace"], app)["spec"]["podSelector"]
        assert selected == {"matchLabels": {"app.kubernetes.io/name": app}}
    # A Role que o despachante e a web usam cobre o que os lançadores pedem à API.
    role = render.get("Role", "rapa-pods-de-uso-unico", values["namespace"])
    granted = {(res, verb) for r in role["rules"] for res in r["resources"] for verb in r["verbs"]}
    for resource in ("pods", "secrets"):
        for verb in ("create", "get", "list", "delete"):
            assert (resource, verb) in granted
    binding = render.get("RoleBinding", "rapa-pods-de-uso-unico", values["namespace"])
    assert {(s["name"], s["namespace"]) for s in binding["subjects"]} == {
        ("rapa-despachante", "rapa"),
        ("rapa-web", "rapa"),
    }
    # O pod de execução sai para o canal do despachante (pelo seletor dele, na porta do
    # canal), e o despachante recebe dele nessa porta, pelo nome que o contêiner dá a ela.
    channel_port = ServerSettings.from_env(dispatcher).channel_port
    egress = _policy(render, values["namespace"], values["run"])["spec"]["egress"]
    to_dispatcher = [
        rule
        for rule in egress
        if any(
            peer.get("podSelector", {}).get("matchLabels", {}).get("app.kubernetes.io/component")
            == "despachante"
            for peer in rule["to"]
        )
    ]
    assert [rule["ports"] for rule in to_dispatcher] == [
        [{"protocol": "TCP", "port": channel_port}]
    ]
    ports = {
        p["name"]: p["containerPort"]
        for p in deployment_pod(render, "rapa-despachante")["containers"][0]["ports"]
    }
    assert ports["canal"] == channel_port
    ingress = _policy(render, "rapa", "rapa-despachante")["spec"]["ingress"]
    from_runs = next(rule for rule in ingress if rule["ports"][0]["port"] == "canal")
    assert from_runs["from"][0]["podSelector"] == {
        "matchLabels": {"app.kubernetes.io/name": values["run"]}
    }
    # A sessão: a web conecta no WebSocket do pod, na porta do host de sessão.
    session_port = int(url.rsplit(":", 1)[1].rstrip("/"))
    (container,) = session["spec"]["containers"]
    assert [p["containerPort"] for p in container["ports"]] == [session_port]
    session_ingress = _policy(render, values["namespace"], values["session"])["spec"]["ingress"]
    assert [rule["ports"] for rule in session_ingress] == [
        [{"protocol": "TCP", "port": session_port}]
    ]
    web_egress = _policy(render, "rapa", "rapa-web")["spec"]["egress"]
    assert any(
        rule.get("ports") == [{"protocol": "TCP", "port": session_port}]
        and rule["to"][0]["podSelector"]
        == {"matchLabels": {"app.kubernetes.io/name": values["session"]}}
        for rule in web_egress
    )


def _script_roles() -> dict[str, tuple[str, str]]:
    """Papel -> (a conta de serviço padrão, a política), como o preparar.sh os escreve."""
    text = (REPO / "deploy" / "openbao" / "preparar.sh").read_text(encoding="utf-8")
    defaults = dict(re.findall(r'^(\w+)="\$\{\w+:-([^}]+)\}"$', text, flags=re.MULTILINE))
    return {
        role: (defaults[variable], policy)
        for role, variable, policy in re.findall(
            r'^role (\S+) "\$(\w+)" (\S+)$', text, flags=re.MULTILINE
        )
    }


@pytest.mark.parametrize("variant", sorted(_VARIANTS))
def test_each_component_logs_in_to_openbao_with_its_own_role(variant: str) -> None:
    """O RAPA_BAO_PAPEL de cada pod do chart é um papel do preparar.sh, ligado à conta de
    serviço desse pod (e só a ela), com a audiência do token projetado; a interface e a API
    só desembrulham."""
    render = _render(variant)
    roles = _script_roles()
    text = (REPO / "deploy" / "openbao" / "preparar.sh").read_text(encoding="utf-8")
    audience = re.search(r'AUDIENCE="\$\{RAPA_BAO_AUDIENCIA:-([^}]+)\}"', text)
    seen = set()
    for name, spec in render.pod_specs():
        if name.startswith("Job/"):
            continue  # a migração não fala com o cofre
        env = {e["name"]: e.get("value") for e in spec["containers"][0].get("env", [])}
        role = env.get("RAPA_BAO_PAPEL")
        if role is None:
            continue
        account, policy = roles[role]
        assert spec["serviceAccountName"] == account, name
        projected = next(v for v in spec["volumes"] if v["name"] == "bao-token")["projected"]
        token = projected["sources"][0]["serviceAccountToken"]
        assert audience and token["audience"] == audience[1], name
        if role in ("rapa-web", "rapa-api"):
            assert policy == "rapa-api"
            assert env.get("RAPA_COFRE") == "nenhum", name
        seen.add(role)
    # Com o OpenBao para as chaves e os segredos, todos os papéis entram em uso.
    assert seen == {"rapa-despachante", "rapa-web", "rapa-api", "rapa-operacao"}
    policy = (REPO / "deploy" / "openbao" / "politicas" / "rapa-api.hcl").read_text()
    assert "rapa/data" not in policy and "encrypt" not in policy


def test_the_operation_pod_reaches_everything_a_company_has_outside_the_database(
    tmp_path: Path,
) -> None:
    """O pod da operação (o CLI de exportar e apagar) monta, com o ambiente do chart, o store
    de objetos e o destruidor que apaga os segredos do KV e a chave transit: apagar uma
    empresa não cai no destruidor local (que só registra)."""
    from rapa.control.artifacts import S3ArtifactStore
    from rapa.control.backends import Backends, BackendSettings
    from rapa.control.db import Database
    from rapa.control.vault import OpenBaoKeyDestroyer

    render = _render("producao")
    spec = render.get("PodTemplate", "rapa-operacao")["template"]["spec"]
    raw = container_env(spec)
    env = {
        name: value if isinstance(value, str) else _FROM_SECRETS.get(name, "valor-de-um-secret")
        for name, value in raw.items()
    }
    settings = BackendSettings.from_env(env)
    backends = Backends(settings)
    store = backends.artifact_store(tmp_path, Database.from_url(env["RAPA_DATABASE_URL"]))
    destroyer = backends.key_destroyer(store)
    assert isinstance(store, S3ArtifactStore)
    assert isinstance(destroyer, OpenBaoKeyDestroyer)
    assert destroyer.destroys_secrets and destroyer.destroys_transit_keys
    assert env["RAPA_BAO_PAPEL"] == "rapa-operacao"
    assert env["RAPA_OWNER_DATABASE_URL"].startswith("postgresql://")
