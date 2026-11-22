"""O Argo CD (deploy/argocd) e o realm de exemplo do Keycloak (deploy/keycloak)."""

import json
import os
import re
import shutil
import socket
import subprocess
import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.deploy.support import CHART, REPO, container_env, kubeconform, template


def applications() -> dict[str, dict[str, Any]]:
    text = (REPO / "deploy" / "argocd" / "application.yaml").read_text(encoding="utf-8")
    return {d["metadata"]["name"]: d for d in yaml.safe_load_all(text) if d}


def test_two_applications_of_the_chart_with_environment_values() -> None:
    apps = applications()
    assert set(apps) == {"rapa", "rapa-banco"}
    for app in apps.values():
        source = app["spec"]["source"]
        assert source["path"] == "deploy/helm/rapa"
        for name in source["helm"]["valueFiles"]:
            assert (CHART / name).exists(), name
        assert app["spec"]["syncPolicy"]["automated"]["selfHeal"] is True
    assert apps["rapa-banco"]["spec"]["source"]["helm"]["valueFiles"][-1] == "values-banco.yaml"
    assert "values-banco.yaml" not in apps["rapa"]["spec"]["source"]["helm"]["valueFiles"]


def test_database_is_never_pruned_and_syncs_first() -> None:
    apps = applications()
    assert apps["rapa-banco"]["spec"]["syncPolicy"]["automated"]["prune"] is False
    assert apps["rapa"]["spec"]["syncPolicy"]["automated"]["prune"] is True
    wave = {
        n: int(a["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"])
        for n, a in apps.items()
    }
    assert wave["rapa-banco"] < wave["rapa"]


def _argocd(name: str) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((REPO / "deploy" / "argocd" / name).read_text("utf-8"))
    return data


def test_the_waves_run_inside_the_root_application() -> None:
    """As ondas só ordenam recursos dentro do sync de uma Application: a raiz ("app of apps")
    cria as duas filhas, o banco antes, e só passa de onda com a filha saudável."""
    root = _argocd("raiz.yaml")
    assert root["kind"] == "Application" and root["metadata"]["namespace"] == "argocd"
    source = root["spec"]["source"]
    assert source["path"] == "deploy/argocd"
    assert source["directory"] == {"include": "application.yaml"}
    assert source["repoURL"] == applications()["rapa"]["spec"]["source"]["repoURL"]
    assert root["spec"]["destination"]["namespace"] == "argocd"
    # Tirar uma filha do arquivo nunca apaga a Application (nem o banco).
    assert root["spec"]["syncPolicy"]["automated"]["prune"] is False
    for app in applications().values():
        assert app["metadata"]["namespace"] == "argocd"


def test_argocd_waits_for_the_database_to_be_ready() -> None:
    """Sem a checagem de saúde de argoproj.io/Application (o Argo CD tirou a dele na 1.8), a
    raiz não espera a filha; a da filha do banco é a do Cluster do CloudNativePG (Ready)."""
    config = _argocd("argocd-cm-saude.yaml")
    assert (config["kind"], config["metadata"]) == (
        "ConfigMap",
        {"name": "argocd-cm", "namespace": "argocd"},
    )
    application = config["data"]["resource.customizations.health.argoproj.io_Application"]
    assert "obj.status.health.status" in application and 'hs.status = "Progressing"' in application
    cluster = config["data"]["resource.customizations.health.postgresql.cnpg.io_Cluster"]
    assert 'condition.type == "Ready"' in cluster and 'hs.status = "Healthy"' in cluster
    for script in (application, cluster):
        assert script.strip().endswith("return hs")
    raiz = (REPO / "deploy" / "argocd" / "raiz.yaml").read_text(encoding="utf-8")
    assert "kubectl -n argocd patch configmap argocd-cm --type merge" in raiz
    operation = (REPO / "docs" / "OPERACAO.md").read_text(encoding="utf-8")
    assert "deploy/argocd/raiz.yaml" in operation and "argocd-cm-saude.yaml" in operation


def _section(text: str, title: str) -> str:
    start = text.index(title)
    end = text.find("\n### ", start + len(title))
    return text[start : end if end != -1 else len(text)]


def test_database_restore_can_be_followed_on_argo_cd() -> None:
    """A restauração de verdade (1.5) na instalação pelo Argo CD: a raiz (automated com
    selfHeal) é dona das Applications filhas e desfaz em segundos o sync desligado nelas, e
    a filha rapa desfaz o scale; e não há release do Helm (o Argo CD renderiza o chart), então
    o banco e a aplicação se atualizam pelo sync, não pelo helm upgrade."""
    raiz = yaml.safe_load((REPO / "deploy" / "argocd" / "raiz.yaml").read_text("utf-8"))
    assert raiz["spec"]["syncPolicy"]["automated"]["selfHeal"] is True
    text = (REPO / "docs" / "OPERACAO.md").read_text(encoding="utf-8")
    restore = _section(text, "### 1.5 Backup e restauração do banco")
    stop = restore.index("argocd app set rapa-raiz --sync-policy none")
    assert stop < restore.index("argocd app set rapa --sync-policy none")
    assert stop < restore.index("argocd app set rapa-banco --sync-policy none")
    assert stop < restore.index("--replicas=0")
    assert "argocd app sync rapa-banco" in restore and "argocd app sync rapa\n" in restore
    # A raiz volta por último (ela devolve às filhas o syncPolicy do repositório).
    assert restore.rindex("argocd app set rapa-raiz --sync-policy automated") > restore.index(
        "argocd app sync rapa\n"
    )


def test_gitops_manifests_match_the_schemas() -> None:
    for name in ("raiz.yaml", "argocd-cm-saude.yaml"):
        text = (REPO / "deploy" / "argocd" / name).read_text(encoding="utf-8")
        assert kubeconform(text) == [], name


def realm() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (REPO / "deploy" / "keycloak" / "realm-rapa.json").read_text(encoding="utf-8")
    )
    return data


def _client(client_id: str) -> dict[str, Any]:
    return next(c for c in realm()["clients"] if c["clientId"] == client_id)


def test_realm_has_no_secret_user_or_organization() -> None:
    data = realm()
    assert data["organizationsEnabled"] is True
    assert "users" not in data and "organizations" not in data
    text = json.dumps(data).lower()
    for client in data["clients"]:
        assert "secret" not in client
    assert '"password"' not in text and "credentials" not in text


def test_interface_client_is_confidential_with_pkce_only() -> None:
    client = _client("rapa-interface")
    assert client["publicClient"] is False
    assert client["standardFlowEnabled"] is True
    assert client["implicitFlowEnabled"] is False
    assert client["directAccessGrantsEnabled"] is False
    assert client["attributes"]["pkce.code.challenge.method"] == "S256"
    assert client["redirectUris"] == ["https://rapa.exemplo.test/auth/oidc/retorno"]


def test_tokens_carry_the_api_audience_and_one_organization() -> None:
    mappers = {
        m["protocolMapper"]: m["config"] for m in _client("rapa-interface")["protocolMappers"]
    }
    assert mappers["oidc-audience-mapper"]["included.client.audience"] == "rapa-api"
    assert mappers["oidc-audience-mapper"]["access.token.claim"] == "true"
    api = _client("rapa-api")
    assert not any(
        api[k]
        for k in (
            "standardFlowEnabled",
            "implicitFlowEnabled",
            "directAccessGrantsEnabled",
            "serviceAccountsEnabled",
        )
    )


def test_tokens_carry_the_organization_from_the_organization_scope() -> None:
    """O mapper de organização do Keycloak 26 (OrganizationMembershipMapper) só põe a claim
    quando o escopo ``organization`` está no pedido (ou o login escolheu uma organização):
    o cliente da interface leva o escopo entre os padrão, sem um segundo mapper no próprio
    cliente (dois mappers da mesma claim correm: o Keycloak os aplica numa ordem qualquer)."""
    client = _client("rapa-interface")
    assert "organization" in client["defaultClientScopes"]
    assert "organization" not in client["optionalClientScopes"]
    assert not any(
        m["protocolMapper"] == "oidc-organization-membership-mapper"
        or m.get("config", {}).get("claim.name") == "organization"
        for m in client["protocolMappers"]
    )
    # O realm não declara clientScopes: o Keycloak cria os padrão, o organization entre eles.
    assert "clientScopes" not in realm()
    # Com o id na claim ({"apelido": {"id": "<uuid>"}}), a forma que o RaPA liga pelo banco.
    readme = (REPO / "deploy" / "keycloak" / "README.md").read_text(encoding="utf-8")
    assert "Add organization id" in readme


def test_keycloak_steps_point_to_the_company_cli() -> None:
    text = (REPO / "deploy" / "keycloak" / "README.md").read_text(encoding="utf-8")
    assert "python -m rapa.control.admin empresa criar" in text
    assert "--organizacao ID_DA_ORGANIZACAO" in text


def test_install_requires_a_cni_that_enforces_network_policies() -> None:
    """Todo o isolamento dos pods de execução e de sessão (o metadata, a API do Kubernetes, o
    banco, os outros pods) vem das NetworkPolicies, e um CNI que não as aplica as ignora
    calado: o Amazon VPC CNI do EKS só as aplica com enableNetworkPolicy. A ordem de
    instalação pede isso antes do chart, e o teste de saída diz o que fazer quando abre."""
    text = (REPO / "docs" / "OPERACAO.md").read_text(encoding="utf-8")
    install = text[text.index("### 1.2 Ordem de instalação") : text.index("### 1.3 Atualização")]
    nodes = install[: install.index("2. **Componentes externos**")]
    assert "enableNetworkPolicy" in nodes and "NetworkPolicy" in nodes
    check = text[text.index("### 1.9 Conferir a guarda de saída") : text.index("### 1.10")]
    assert "ERRO: abriu" in check and "enableNetworkPolicy" in check


def test_operation_manifests_match_the_schemas() -> None:
    """A RuntimeClass do gVisor e o pod do teste de saída também passam no kubeconform."""
    for name in ("deploy/gvisor/runtimeclass.yaml", "deploy/rede/teste-de-saida.yaml"):
        assert kubeconform((REPO / name).read_text(encoding="utf-8")) == [], name


def test_gvisor_runtime_class_takes_pods_to_the_execution_nodes() -> None:
    runtime = yaml.safe_load((REPO / "deploy/gvisor/runtimeclass.yaml").read_text("utf-8"))
    assert runtime["metadata"]["name"] == "gvisor" and runtime["handler"] == "runsc"
    assert runtime["scheduling"]["nodeSelector"] == {"rapa.dev/execucoes": "sim"}


def test_egress_check_pod_is_a_run_pod() -> None:
    """O teste de saída roda com o rótulo, a conta e a segurança dos pods de execução."""
    pod = yaml.safe_load((REPO / "deploy/rede/teste-de-saida.yaml").read_text("utf-8"))
    assert pod["metadata"]["namespace"] == "rapa-execucoes"
    assert pod["metadata"]["labels"] == {"app.kubernetes.io/name": "rapa-executor"}
    spec = pod["spec"]
    assert spec["serviceAccountName"] == "rapa-executor"
    assert spec["automountServiceAccountToken"] is False
    assert spec["runtimeClassName"] == "gvisor"
    container = spec["containers"][0]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    internal = next(e["value"] for e in container["env"] if e["name"] == "ALVOS_INTERNOS")
    assert "169.254.169.254:80" in internal and "kubernetes.default.svc:443" in internal
    # O metadata também em IPv6 e pelo NAT64, e uma rede privada pelo NAT64.
    for target in ("[fd00:ec2::254]:80", "[64:ff9b::a9fe:a9fe]:80", "[64:ff9b::a00:1]:443"):
        assert target in internal.split(","), target


class _Connection:
    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *_: object) -> None:
        return None


def test_egress_check_tries_each_target_including_ipv6(monkeypatch: pytest.MonkeyPatch) -> None:
    """O programa do teste de saída abre cada alvo (os de IPv6 sem os colchetes), conta como
    erro o interno que abre e o externo que não abre, e soma os alvos do ambiente."""
    pod = yaml.safe_load((REPO / "deploy/rede/teste-de-saida.yaml").read_text("utf-8"))
    container = pod["spec"]["containers"][0]
    assert container["args"][:2] == ["python", "-c"]
    program = container["args"][2]
    env = {e["name"]: e["value"] for e in container["env"]}
    tried: list[tuple[str, int]] = []
    opened = {"example.com", "2001:db8::1"}

    def connect(address: tuple[str, int], timeout: float) -> _Connection:
        tried.append(address)
        if address[0] in opened:
            return _Connection()
        raise OSError("fechado")

    monkeypatch.setattr(socket, "create_connection", connect)

    def run(**changes: str) -> int:
        tried.clear()
        for name, value in {**env, **changes}.items():
            monkeypatch.setenv(name, value)
        with pytest.raises(SystemExit) as finished:
            exec(compile(program, "teste-de-saida", "exec"), {"__name__": "__main__"})  # noqa: S102
        return int(finished.value.code or 0)

    assert run() == 0
    assert ("fd00:ec2::254", 80) in tried and ("64:ff9b::a9fe:a9fe", 80) in tried
    assert ("169.254.169.254", 80) in tried and ("example.com", 443) in tried
    # Um interno do ambiente que abre é erro; um externo de IPv6 que abre, não.
    assert run(ALVOS_DO_AMBIENTE="[2001:db8::1]:8000") == 1
    assert run(ALVOS_EXTERNOS_IPV6="[2001:db8::1]:443") == 0
    assert ("2001:db8::1", 443) in tried
    assert run(ALVOS_EXTERNOS_IPV6="[2001:db8::2]:443") == 1


def test_restore_test_never_writes_to_the_backups() -> None:
    """O Cluster restaurado só lê os backups: nunca arquiva WAL nem mexe na origem."""
    script = (REPO / "deploy/banco/testar-restauracao.sh").read_text(encoding="utf-8")
    assert "externalClusters:" in script and "barmanObjectName" in script
    assert "isWALArchiver" not in script and "plugins:" not in script
    assert "ScheduledBackup" not in script
    # Na origem, só leituras (get); tudo o que é criado vai para o namespace do teste.
    for line in script.splitlines():
        if '-n "$origem_ns"' in line:
            assert " get " in line, line


#: Um kubectl falso para o teste de restauração: registra cada chamada (e o que chega pela
#: entrada) e responde como um cluster com o ObjectStore, os Secrets e o banco restaurado.
FAKE_KUBECTL = """\
#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
data = sys.stdin.read() if "-f" in args and "-" in args else ""
with open(os.environ["REGISTRO"], "a", encoding="utf-8") as log:
    log.write(json.dumps({"args": args, "stdin": data}) + "\\n")
def get(flag):
    return args[args.index(flag) + 1] if flag in args else None
if args[:2] == ["create", "namespace"]:
    print("apiVersion: v1\\nkind: Namespace\\nmetadata:\\n  name: " + args[2])
elif "get" in args:
    kind, name = args[args.index("get") + 1], args[args.index("get") + 2]
    output = get("-o") or ""
    if output.startswith("jsonpath="):
        print({"{.spec.configuration.s3Credentials.accessKeyId.name}": "rapa-banco-backup-s3",
               "{.spec.configuration.endpointCA.name}": "rapa-s3-ca"}[output[9:]], end="")
    else:
        print(json.dumps({"apiVersion": "v1", "kind": kind, "metadata": {
            "name": name, "namespace": get("-n"), "uid": "u-1", "resourceVersion": "7",
            "ownerReferences": [{"kind": "Cluster", "name": "rapa-banco"}]},
            "data": {"segredo": "eA=="}, "status": {"x": 1}}))
elif "exec" in args:
    query = args[-1]
    if "alembic_version" in query:
        print(os.environ.get("REVISAO", "v0009"))
    elif "max(" in query:
        print("2026-11-20 03:00:00+00")
    else:
        print("3")
"""


def _restore(tmp_path: Path, **env: str) -> tuple[subprocess.CompletedProcess[str], list[Any]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    kubectl = bin_dir / "kubectl"
    kubectl.write_text(FAKE_KUBECTL, encoding="utf-8")
    kubectl.chmod(0o755)
    log = tmp_path / "chamadas.jsonl"
    log.unlink(missing_ok=True)
    bash = shutil.which("bash")
    assert bash is not None
    process = subprocess.run(  # noqa: S603 - o script do repositório, com um kubectl falso
        [bash, str(REPO / "deploy/banco/testar-restauracao.sh")],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "REGISTRO": str(log), **env},
    )
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    return process, calls


def _applied(calls: list[Any]) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    for call in calls:
        if "apply" in call["args"]:
            docs += [d for d in yaml.safe_load_all(call["stdin"]) if d]
    return docs


def test_restore_test_runs_against_the_backups_with_the_real_owner(tmp_path: Path) -> None:
    """O script inteiro, contra um kubectl falso: o Cluster restaurado nasce dos backups com o
    dono que está neles (rapa_owner; sem ele, o operador assume um papel "rapa" que não existe
    e o Cluster não fica pronto), a origem só é lida e o resultado diz quanto levou."""
    process, calls = _restore(tmp_path, ALVO_HORA="2026-11-20 03:00:00+00")
    assert process.returncode == 0, process.stderr
    last = process.stdout.strip().splitlines()[-1]
    assert re.fullmatch(r"restauração conferida em \d+s: revisão v0009, 3 empresas, .+", last)
    docs = _applied(calls)
    cluster = next(d for d in docs if d["kind"] == "Cluster")
    assert cluster["metadata"]["name"] == "rapa-banco-restaurado"
    recovery = cluster["spec"]["bootstrap"]["recovery"]
    assert recovery == {
        "source": "origem",
        "database": "rapa",
        "owner": "rapa_owner",
        "recoveryTarget": {"targetTime": "2026-11-20 03:00:00+00"},
    }
    assert cluster["spec"]["externalClusters"] == [
        {
            "name": "origem",
            "plugin": {
                "name": "barman-cloud.cloudnative-pg.io",
                "parameters": {"barmanObjectName": "rapa-banco-backup", "serverName": "rapa-banco"},
            },
        }
    ]
    assert "plugins" not in cluster["spec"]
    # O que vem da origem chega ao namespace do teste sem os metadados dela.
    assert docs[0] == {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": "rapa-restauracao"},
    }
    copies = [d for d in docs[1:] if d["kind"] != "Cluster"]
    assert {(d["kind"], d["metadata"]["name"]) for d in copies} == {
        ("secret", "rapa-banco-backup-s3"),
        ("secret", "rapa-s3-ca"),
        ("objectstore", "rapa-banco-backup"),
    }
    for copy in copies:
        assert copy["metadata"] == {
            "name": copy["metadata"]["name"],
            "namespace": "rapa-restauracao",
        }
        assert "status" not in copy
    # Na origem, só leituras; no fim, o namespace do teste sai.
    for call in calls:
        if call["args"][:2] == ["-n", "rapa"]:
            assert call["args"][2] == "get", call["args"]
    assert calls[-1]["args"][:3] == ["delete", "namespace", "rapa-restauracao"]


def test_restore_test_fails_without_the_migrations_table(tmp_path: Path) -> None:
    process, calls = _restore(tmp_path, REVISAO="")
    assert process.returncode == 1
    assert "não tem a tabela de migrações" in process.stderr
    assert calls[-1]["args"][:3] == ["delete", "namespace", "rapa-restauracao"]
    assert "owner: rapa_owner" in "".join(c["stdin"] for c in calls)


def _operation_doc() -> str:
    return (REPO / "docs" / "OPERACAO.md").read_text(encoding="utf-8")


def _section(text: str, title: str) -> str:
    start = text.index(title)
    end = text.find("\n### ", start + len(title))
    return text[start : end if end != -1 else None]


def test_restore_procedure_values_render_the_new_cluster(tmp_path: Path) -> None:
    """Os valores da restauração de verdade (1.5) sobem um Cluster novo pela release do banco
    e levam as NetworkPolicies da aplicação até ele (o nega-tudo do namespace fecharia um
    Cluster criado à parte)."""
    section = _section(_operation_doc(), "### 1.5 Backup e restauração do banco")
    block = re.search(r"```yaml\n(.*?)```", section, re.S)
    assert block is not None
    restore = tmp_path / "restauracao.yaml"
    restore.write_text(textwrap.dedent(block.group(1)), encoding="utf-8")
    banco = template("values-producao-exemplo.yaml", "values-banco.yaml", str(restore))
    cluster = banco.get("Cluster", "rapa-banco-2")["spec"]
    assert cluster["bootstrap"]["recovery"]["owner"] == "rapa_owner"
    assert cluster["externalClusters"][0]["plugin"]["parameters"]["serverName"] == "rapa-banco"
    app = template("values-producao-exemplo.yaml", str(restore))
    env = container_env(app.get("Deployment", "rapa-api")["spec"]["template"]["spec"])
    assert "@rapa-banco-2-pooler-rw:5432/" in env["RAPA_DATABASE_URL"]
    assert "sslrootcert=/var/run/rapa/banco/ca.crt" in env["RAPA_DATABASE_URL"]
    volumes = app.get("Deployment", "rapa-api")["spec"]["template"]["spec"]["volumes"]
    ca = next(v for v in volumes if v["name"] == "banco-ca")
    assert ca["secret"]["secretName"] == "rapa-banco-2-ca"
    selectors = [
        peer["podSelector"]["matchLabels"]
        for policy in app.of_kind("NetworkPolicy")
        if policy["metadata"]["name"] == "rapa-api"
        for rule in policy["spec"]["egress"]
        for peer in rule.get("to", [])
        if "podSelector" in peer and "namespaceSelector" not in peer
    ]
    assert {"cnpg.io/cluster": "rapa-banco-2"} in selectors
    assert "kubectl -n rapa wait cluster/rapa-banco-2" in section


def test_owner_commands_run_in_the_operation_pod() -> None:
    """Congelar, exportar e apagar rodam no pod da operação (o papel do OpenBao que apaga os
    segredos e a chave da empresa, e um disco próprio), nunca na api que serve tráfego."""
    section = _section(_operation_doc(), "### 1.10 O CLI no cluster")
    assert "kubectl -n rapa get podtemplate rapa-operacao" in section
    assert "kubectl -n rapa exec rapa-operacao -- python -m rapa.control.admin" in section
    assert "--saida /trabalho/empresa.tar.gz" in section
    assert "kubectl -n rapa delete pod rapa-operacao" in section
    assert "deploy/rapa-api" not in section
    keycloak = (REPO / "deploy" / "keycloak" / "README.md").read_text(encoding="utf-8")
    assert "kubectl -n rapa exec rapa-operacao -- python -m rapa.control.admin" in keycloak


def test_sealed_openbao_and_s3_down_take_every_route_down() -> None:
    """A prontidão da web e da api é /saude/pronto, que confere o OpenBao e o S3: com um deles
    fora, as réplicas saem dos Services e o gateway responde 503 a tudo. O roteiro diz isso."""
    for component in ("web", "api"):
        pod = template("values-producao-exemplo.yaml").get("Deployment", f"rapa-{component}")
        probe = pod["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]
        assert probe["httpGet"]["path"] == "/saude/pronto"
    incidents = _section(_operation_doc(), "### 1.8 Incidentes")
    sealed = next(line for line in incidents.splitlines() if "**OpenBao selado.**" in line)
    down = next(line for line in incidents.splitlines() if "**S3 fora.**" in line)
    for line in (sealed, down):
        assert "saem dos Services" in line and "503 a tudo" in line


def test_the_issuer_reaches_keycloak_inside_the_cluster() -> None:
    """O emissor é um nome público: dentro do cluster, ele precisa chegar ao Service do
    Keycloak (a NetworkPolicy só abre o namespace identidade), não ao Envoy."""
    keycloak = (REPO / "deploy" / "keycloak" / "README.md").read_text(encoding="utf-8")
    assert "rewrite name exact sso.exemplo.test keycloak.identidade.svc.cluster.local" in keycloak
    values = yaml.safe_load((CHART / "values-producao-exemplo.yaml").read_text("utf-8"))
    assert values["auth"]["jwt"]["issuer"].startswith("https://sso.exemplo.test/")
    default = yaml.safe_load((CHART / "values.yaml").read_text("utf-8"))
    assert default["network"]["egress"]["keycloak"]["to"] == [
        {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "identidade"}}}
    ]
