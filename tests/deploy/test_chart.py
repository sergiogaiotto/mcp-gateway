"""O chart do RaPA (deploy/helm/rapa): lint, render, esquemas e as regras dos manifestos.

Cada regra do chart tem um teste aqui: a segurança restrita de todo pod, nenhum valor de
segredo no render, as NetworkPolicies que negam tudo por padrão, o Job de migração como hook,
as rotas, o RBAC, a saúde do despachante na porta do canal e as recusas da validação.
"""

import functools
import ipaddress
import re
from typing import Any

import pytest

from tests.deploy.support import (
    CHART,
    KUBE_VERSION,
    VARIANTS,
    Render,
    container_env,
    deployment_pod,
    kubeconform,
    run_helm,
    template,
    template_error,
)

APP_VARIANTS = ("padrao", "kind", "producao")
COMPONENTS = ("web", "api", "despachante")


@functools.cache
def rendered(variant: str) -> Render:
    return template(*VARIANTS[variant])


def _env(variant: str, component: str) -> dict[str, Any]:
    return _env_of(rendered(variant), component)


def _env_of(render: Render, component: str) -> dict[str, Any]:
    return container_env(deployment_pod(render, f"rapa-{component}"))


# ---------------------------------------------------------------------------------------------
# Lint e esquemas
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("variant", sorted(VARIANTS))
def test_lint_and_schemas(variant: str) -> None:
    """helm lint --strict, helm template e kubeconform -strict (com os CRDs) em cada conjunto."""
    values = VARIANTS[variant]
    args = [a for name in values for a in ("-f", str(CHART / name))]
    lint = run_helm("lint", "--strict", str(CHART), "--kube-version", KUBE_VERSION, *args)
    assert lint.returncode == 0, lint.stdout + lint.stderr
    render = rendered(variant)
    assert render.docs
    assert kubeconform(render.text) == []


def test_gitops_application_matches_the_schema() -> None:
    """O Application do Argo CD também passa no kubeconform (com o esquema do CRD)."""
    text = (CHART.parents[1] / "argocd" / "application.yaml").read_text(encoding="utf-8")
    assert kubeconform(text) == []


# ---------------------------------------------------------------------------------------------
# Segurança dos pods
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("variant", sorted(VARIANTS))
def test_every_pod_is_restricted(variant: str) -> None:
    """Não root, seccomp, só leitura, sem capacidades, sem escalar, nada do host."""
    pods = list(rendered(variant).pod_specs())
    if variant.startswith("banco"):
        assert pods == []  # o banco é do operador
        return
    assert pods
    for owner, pod in pods:
        context = pod["securityContext"]
        assert context["runAsNonRoot"] is True, owner
        assert context["runAsUser"] == 10001, owner
        assert context["seccompProfile"] == {"type": "RuntimeDefault"}, owner
        assert pod["enableServiceLinks"] is False, owner
        for key in ("hostNetwork", "hostPID", "hostIPC"):
            assert not pod.get(key), owner
        assert all("hostPath" not in volume for volume in pod.get("volumes", [])), owner
        for container in pod["containers"] + pod.get("initContainers", []):
            sc = container["securityContext"]
            assert sc["runAsNonRoot"] is True, owner
            assert sc["allowPrivilegeEscalation"] is False, owner
            assert sc["privileged"] is False, owner
            assert sc["readOnlyRootFilesystem"] is True, owner
            assert sc["capabilities"] == {"drop": ["ALL"]}, owner
            assert sc["seccompProfile"] == {"type": "RuntimeDefault"}, owner
            assert "resources" in container and container["resources"].get("limits"), owner
            # O disco é só de leitura: /tmp vem de um emptyDir.
            mounts = {m["mountPath"] for m in container.get("volumeMounts", [])}
            assert "/tmp" in mounts, owner


@pytest.mark.parametrize("variant", APP_VARIANTS)
def test_service_account_token_only_where_pods_are_created(variant: str) -> None:
    """Só o despachante e a web (que criam os pods de uso único) têm o token montado."""
    render = rendered(variant)
    mounted = {owner for owner, pod in render.pod_specs() if pod["automountServiceAccountToken"]}
    assert mounted == {"Deployment/rapa-despachante", "Deployment/rapa-web"}
    accounts = {
        (sa["metadata"]["namespace"], sa["metadata"]["name"]): sa["automountServiceAccountToken"]
        for sa in render.of_kind("ServiceAccount")
    }
    assert accounts[("rapa", "rapa-api")] is False
    assert accounts[("rapa", "rapa-operacao")] is False
    assert accounts[("rapa-execucoes", "rapa-executor")] is False
    # A migração usa a conta padrão do namespace, sem o token.
    job = render.get("Job", "rapa-migracao")["spec"]["template"]["spec"]
    assert "serviceAccountName" not in job and job["automountServiceAccountToken"] is False


def test_executions_namespace_enforces_restricted_pod_security() -> None:
    namespace = rendered("producao").get("Namespace", "rapa-execucoes")
    labels = namespace["metadata"]["labels"]
    assert labels["pod-security.kubernetes.io/enforce"] == "restricted"
    assert namespace["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"


def test_executions_namespace_is_never_the_control_namespace() -> None:
    error = template_error(sets={"executions.namespace": "rapa"})
    assert "executions.namespace" in error


# ---------------------------------------------------------------------------------------------
# Segredos
# ---------------------------------------------------------------------------------------------

#: Variáveis que levam segredo: só por secretKeyRef, nunca com o valor no manifesto.
SECRET_VARIABLES = {
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "RAPA_CHAVE_ARTEFATOS",
    "RAPA_CHAVE_WEBHOOKS",
    "RAPA_GLITCHTIP_DSN",
    "RAPA_LLM_API_KEY",
    "RAPA_OIDC_CLIENT_SECRET",
}


def _all_envs(render: Render) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for owner, pod in render.pod_specs():
        for container in pod["containers"]:
            for entry in container.get("env", []):
                out.append((owner, entry))
    return out


@pytest.mark.parametrize("variant", sorted(VARIANTS))
def test_no_secret_value_in_the_render(variant: str) -> None:
    """Nenhum Secret no chart; os segredos só por secretKeyRef; a senha do banco por $(VAR)."""
    render = rendered(variant)
    assert render.of_kind("Secret") == []
    for owner, entry in _all_envs(render):
        name = entry["name"]
        if name in SECRET_VARIABLES or name.startswith("BANCO_SENHA_"):
            assert "value" not in entry, (owner, name)
            assert set(entry["valueFrom"]) == {"secretKeyRef"}, (owner, name)
        if name.endswith("DATABASE_URL"):
            url = entry["value"]
            assert re.fullmatch(r"postgresql://\w+:\$\(BANCO_SENHA_[A-Z]+\)@[^?]+\?.+", url), url
        elif isinstance(entry.get("value"), str):
            # Nenhuma outra URL com usuário e senha (um proxy com senha vem de um Secret).
            assert not re.search(r"://[^/?#\s]*@", entry["value"]), (owner, name)
    assert "PRIVATE KEY" not in render.text


@pytest.mark.parametrize(
    "key",
    [
        "executions.egress.proxy.url",
        "observability.otlpEndpoint",
        "modelHub.baseUrl",
        "vault.address",
        "storage.s3.endpoint",
        "auth.jwt.jwksUrl",
    ],
)
def test_urls_never_carry_credentials(key: str) -> None:
    """Um usuário e uma senha na URL iriam em claro para o manifesto e para os pods."""
    error = template_error(
        "values-producao-exemplo.yaml",
        sets={key: "https://usuario:senha@destino.exemplo.test:3128"},
    )
    assert key in error and "usuário e senha" in error
    assert "senha@" not in error


def test_proxy_with_a_password_comes_from_a_secret() -> None:
    render = template(
        "values-producao-exemplo.yaml",
        sets={
            "executions.egress.proxy.url": "",
            "executions.egress.proxy.existingSecret": "rapa-proxy",
        },
    )
    for component in ("web", "despachante"):
        env = container_env(deployment_pod(render, f"rapa-{component}"))
        assert env["RAPA_PROXY_SAIDA"] == {"secretKeyRef": {"name": "rapa-proxy", "key": "url"}}
    # A NetworkPolicy continua abrindo o proxy, na porta dele.
    spec = _policies(render, "rapa-execucoes")["rapa-executor"]
    assert any(
        rule.get("ports") == [{"protocol": "TCP", "port": 3128}]
        and rule["to"] == [{"ipBlock": {"cidr": "10.20.0.10/32"}}]
        for rule in spec["egress"]
    )
    both = template_error(
        "values-producao-exemplo.yaml",
        sets={"executions.egress.proxy.existingSecret": "rapa-proxy"},
    )
    assert "url ou existingSecret" in both


def test_database_urls_use_each_role_password() -> None:
    env = _env("producao", "despachante")
    assert env["RAPA_DATABASE_URL"].startswith("postgresql://rapa_app:$(BANCO_SENHA_APP)@")
    assert env["RAPA_DISPATCHER_DATABASE_URL"].startswith(
        "postgresql://rapa_dispatcher:$(BANCO_SENHA_DISPATCHER)@"
    )
    assert env["BANCO_SENHA_APP"]["secretKeyRef"]["name"] == "rapa-banco-app"
    # A senha precisa vir antes da URL: a expansão $(VAR) só vê as variáveis anteriores.
    pod = deployment_pod(rendered("producao"), "rapa-despachante")
    names = [e["name"] for e in pod["containers"][0]["env"]]
    assert names.index("BANCO_SENHA_APP") < names.index("RAPA_DATABASE_URL")
    assert names.index("BANCO_SENHA_DISPATCHER") < names.index("RAPA_DISPATCHER_DATABASE_URL")
    # O banco com TLS conferido pelo CA do CloudNativePG.
    assert "sslmode=verify-full&sslrootcert=/var/run/rapa/banco/ca.crt" in env["RAPA_DATABASE_URL"]
    # A web e a api nunca recebem a senha do despachante nem a do dono.
    for component in ("web", "api"):
        assert not {"BANCO_SENHA_DISPATCHER", "BANCO_SENHA_OWNER"} & set(
            _env("producao", component)
        )


@pytest.mark.parametrize("mode", ["disable", "prefer", "require"])
def test_database_without_verified_tls_is_refused_outside_dev(mode: str) -> None:
    """require cifra mas não confere o servidor: fora do desenvolvimento, só verify-*."""
    error = template_error(
        "values-producao-exemplo.yaml",
        sets={"database.sslMode": mode, "database.caSecret.name": ""},
    )
    assert "database.sslMode" in error
    error = template_error(sets={"database.caSecret.name": ""})
    assert "database.caSecret.name" in error
    # No kind (desenvolvimento), o modo é livre.
    assert template("values-kind.yaml", sets={"database.sslMode": mode}).docs


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("vault.address", "http://openbao.cofre.svc:8200"),
        ("storage.s3.endpoint", "http://seaweedfs-s3.armazenamento.svc:8333"),
        ("auth.jwt.jwksUrl", "http://sso.exemplo.test/realms/rapa/protocol/openid-connect/certs"),
        ("auth.jwt.issuer", "http://sso.exemplo.test/realms/rapa"),
        ("modelHub.baseUrl", "http://hub.interno.exemplo.test/v1"),
        ("publicUrl", "http://rapa.exemplo.test"),
    ],
)
def test_secrets_only_travel_over_https_outside_dev(key: str, value: str) -> None:
    """O token do OpenBao, as credenciais do S3, as chaves dos tokens e a do hub: só https."""
    error = template_error("values-producao-exemplo.yaml", sets={key: value})
    assert key in error and "https://" in error
    # No kind, o OpenBao de dev e o SeaweedFS de um nó são http.
    kind = _env("kind", "despachante")
    assert kind["RAPA_BAO_ENDERECO"].startswith("http://")
    assert kind["RAPA_S3_ENDERECO"].startswith("http://")


def test_database_backups_only_over_https_outside_dev() -> None:
    error = template_error(
        "values-producao-exemplo.yaml",
        "values-banco.yaml",
        sets={"database.cnpg.backup.endpointURL": "http://seaweedfs-s3.armazenamento.svc:8333"},
    )
    assert "database.cnpg.backup.endpointURL" in error


# ---------------------------------------------------------------------------------------------
# Rede
# ---------------------------------------------------------------------------------------------


def _policies(render: Render, namespace: str) -> dict[str, dict[str, Any]]:
    return {
        p["metadata"]["name"]: p["spec"]
        for p in render.of_kind("NetworkPolicy")
        if p["metadata"]["namespace"] == namespace
    }


def test_network_policies_are_never_off_outside_dev() -> None:
    """Sem as NetworkPolicies, os robôs alcançam o banco, o OpenBao, o S3 e a API do Kubernetes."""
    for values in (
        ("values-producao-exemplo.yaml",),
        ("values-producao-exemplo.yaml", "values-banco.yaml"),
    ):
        error = template_error(*values, sets={"network.policies.enabled": "false"})
        assert "network.policies.enabled=false" in error
    # No kind (desenvolvimento), dá para desligar.
    render = template("values-kind.yaml", sets={"network.policies.enabled": "false"})
    assert render.of_kind("NetworkPolicy") == []


@pytest.mark.parametrize("variant", APP_VARIANTS)
def test_network_policies_deny_everything_by_default(variant: str) -> None:
    render = rendered(variant)
    for namespace in ("rapa", "rapa-execucoes"):
        deny = _policies(render, namespace)["rapa-nega-tudo"]
        assert deny == {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}
    control = _policies(render, "rapa")
    for component in (*COMPONENTS, "migracao"):
        spec = control[f"rapa-{component}"]
        assert spec["podSelector"]["matchLabels"]["app.kubernetes.io/component"] == component
        assert spec["policyTypes"] == ["Ingress", "Egress"]
    # Nenhuma regra do plano de controle abre a internet.
    for spec in control.values():
        for rule in spec.get("egress", []):
            for peer in rule.get("to", []):
                assert peer.get("ipBlock", {}).get("cidr") not in {"0.0.0.0/0", "::/0"}


def test_web_and_api_only_receive_from_the_gateway() -> None:
    render = rendered("producao")
    gateway_pods = [
        {
            "namespaceSelector": {
                "matchLabels": {"kubernetes.io/metadata.name": "envoy-gateway-system"}
            },
            "podSelector": {
                "matchLabels": {
                    "app.kubernetes.io/component": "proxy",
                    "app.kubernetes.io/managed-by": "envoy-gateway",
                }
            },
        }
    ]
    for component in ("web", "api"):
        ingress = _policies(render, "rapa")[f"rapa-{component}"]["ingress"]
        by_port = {rule["ports"][0]["port"]: rule["from"] for rule in ingress}
        assert by_port["http"] == gateway_pods
        assert set(by_port) == {"http", "metricas"}  # e o coletor só na porta de métricas
    # Sem gateway (kind), nada entra pela rede.
    assert _policies(rendered("kind"), "rapa")["rapa-web"]["ingress"] == []


def test_dispatcher_only_receives_from_run_pods_on_the_channel() -> None:
    ingress = _policies(rendered("producao"), "rapa")["rapa-despachante"]["ingress"]
    channel = next(rule for rule in ingress if rule["ports"][0]["port"] == "canal")
    assert channel["from"] == [
        {
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "rapa-execucoes"}},
            "podSelector": {"matchLabels": {"app.kubernetes.io/name": "rapa-executor"}},
        }
    ]
    assert {rule["ports"][0]["port"] for rule in ingress} == {"canal", "metricas"}


def _egress_ports(spec: dict[str, Any]) -> set[int]:
    return {port["port"] for rule in spec.get("egress", []) for port in rule.get("ports", [])}


def test_control_plane_egress_goes_only_to_its_destinations() -> None:
    policies = _policies(rendered("producao"), "rapa")
    dns = {53}
    database, vault, keycloak, s3, hub, kube, obs = (
        {5432},
        {8200},
        {8443, 443},
        {8333},
        {443},
        {443},
        {4317, 4318, 8000},
    )
    assert _egress_ports(policies["rapa-web"]) == (
        dns | database | vault | keycloak | s3 | hub | kube | obs | {8765}
    )
    assert _egress_ports(policies["rapa-api"]) == dns | database | vault | keycloak | s3 | hub | obs
    assert _egress_ports(policies["rapa-despachante"]) == dns | database | vault | s3 | kube | obs
    assert _egress_ports(policies["rapa-migracao"]) == dns | database
    # A api não fala com a API do Kubernetes; o despachante não fala com o Keycloak nem o hub.
    api_peers = [p for r in policies["rapa-api"]["egress"] for p in r.get("to", [])]
    assert {"ipBlock": {"cidr": "10.0.0.0/24"}} not in api_peers
    dispatcher_peers = [p for r in policies["rapa-despachante"]["egress"] for p in r.get("to", [])]
    assert {"ipBlock": {"cidr": "10.40.0.0/24"}} not in dispatcher_peers


def test_a_destination_without_peers_stays_closed() -> None:
    render = template("values-producao-exemplo.yaml", set_json={"network.egress.s3.to": []})
    for name in ("rapa-web", "rapa-api", "rapa-despachante"):
        assert 8333 not in _egress_ports(_policies(render, "rapa")[name])


def test_web_reaches_session_pods_and_session_pods_only_receive_from_the_web() -> None:
    render = rendered("producao")
    web = _policies(render, "rapa")["rapa-web"]["egress"]
    relay = next(rule for rule in web if rule["ports"] == [{"protocol": "TCP", "port": 8765}])
    assert relay["to"] == [
        {
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "rapa-execucoes"}},
            "podSelector": {"matchLabels": {"app.kubernetes.io/name": "rapa-sessao"}},
        }
    ]
    session = _policies(render, "rapa-execucoes")["rapa-sessao"]
    assert session["ingress"] == [
        {
            "from": [
                {
                    "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "rapa"}},
                    "podSelector": {
                        "matchLabels": {
                            "app.kubernetes.io/name": "rapa",
                            "app.kubernetes.io/instance": "rapa",
                            "app.kubernetes.io/component": "web",
                        }
                    },
                }
            ],
            "ports": [{"protocol": "TCP", "port": 8765}],
        }
    ]
    # Nada entra nos pods de execução.
    assert "ingress" not in _policies(render, "rapa-execucoes")["rapa-executor"]


def _nat64(net: str) -> ipaddress.IPv6Network:
    """A rede de IPv4 dentro do prefixo NAT64 64:ff9b::/96."""
    v4 = ipaddress.ip_network(net)
    nat64 = ipaddress.ip_network("64:ff9b::/96")
    return ipaddress.IPv6Network(
        (int(nat64.network_address) | int(v4.network_address), 96 + v4.prefixlen)
    )


def _covered(net: ipaddress.IPv6Network, blocks: list[Any]) -> bool:
    return any(block.version == 6 and net.subnet_of(block) for block in blocks)


INTERNAL_IPV4 = [
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.168.0.0/16",
]


def _internet_rule(spec: dict[str, Any]) -> dict[str, list[str]]:
    for rule in spec["egress"]:
        blocks = [peer["ipBlock"] for peer in rule.get("to", []) if "ipBlock" in peer]
        if any(block["cidr"] == "0.0.0.0/0" for block in blocks):
            assert "ports" not in rule
            return {block["cidr"]: block["except"] for block in blocks}
    raise AssertionError("sem a regra da internet")


@pytest.mark.parametrize("pods", ["rapa-executor", "rapa-sessao"])
def test_robot_pods_reach_the_internet_minus_internal_networks(pods: str) -> None:
    spec = _policies(rendered("producao"), "rapa-execucoes")[pods]
    internet = _internet_rule(spec)
    assert set(internet) == {"0.0.0.0/0", "::/0"}
    assert set(INTERNAL_IPV4) <= set(internet["0.0.0.0/0"])
    v6 = [ipaddress.ip_network(net) for net in internet["::/0"]]
    # Cada rede interna de IPv4 também está fechada dentro do prefixo NAT64.
    for net in internet["0.0.0.0/0"]:
        assert _covered(_nat64(net), v6), net
    for net in ("::1", "fd00:ec2::254", "fe80::1", "2002:a00:1::1"):
        address = ipaddress.ip_address(net)
        assert any(address in block for block in v6), net
    # O IPv4 mapeado (::ffff:10.0.0.1) nunca sai como IPv6: vale a regra de IPv4.
    v4 = [ipaddress.ip_network(net) for net in internet["0.0.0.0/0"]]
    mapped = ipaddress.ip_address("::ffff:10.0.0.1").ipv4_mapped  # type: ignore[union-attr]
    assert any(mapped in block for block in v4)
    # E as do ambiente (o bloco IPv6 global da VPC).
    assert "2001:db8::/32" in internet["::/0"]
    # Toda exceção fica dentro da rede da regra (senão a NetworkPolicy é inválida).
    for cidr, excepts in internet.items():
        parent = ipaddress.ip_network(cidr)
        for item in excepts:
            child = ipaddress.ip_network(item)
            assert child.version == parent.version and child.subnet_of(parent), item  # type: ignore[arg-type]
            # A API do Kubernetes (net.IPNet do Go) lê um IPv4 mapeado como IPv4 e recusa a
            # política: "must be a strict subset of cidr".
            if isinstance(child, ipaddress.IPv6Network):
                assert child.network_address.ipv4_mapped is None, item
    # Fora a internet, só as redes internas permitidas e o proxy (na porta dele).
    others = [
        (peer["ipBlock"]["cidr"], rule.get("ports"))
        for rule in spec["egress"]
        for peer in rule.get("to", [])
        if "ipBlock" in peer and peer["ipBlock"]["cidr"] not in {"0.0.0.0/0", "::/0"}
    ]
    assert sorted(others, key=str) == sorted(
        [("10.30.0.0/24", None), ("10.20.0.10/32", [{"protocol": "TCP", "port": 3128}])], key=str
    )


def test_extra_internal_ipv4_networks_are_also_closed_through_nat64() -> None:
    """Uma rede de IPv4 fora da RFC 1918 (o pool do cliente no Outposts, uma rede da empresa)
    fecha também como 64:ff9b::<v4>, por onde um cluster com NAT64 e DNS64 a alcançaria."""
    render = template(
        "values-producao-exemplo.yaml",
        set_json={"executions.egress.extraInternalNetworks": ["2001:db8::/32", "203.0.113.0/24"]},
    )
    for pods in ("rapa-executor", "rapa-sessao"):
        internet = _internet_rule(_policies(render, "rapa-execucoes")[pods])
        assert "203.0.113.0/24" in internet["0.0.0.0/0"]
        assert "64:ff9b::cb00:7100/120" in internet["::/0"]
        v6 = [ipaddress.ip_network(net) for net in internet["::/0"]]
        assert ipaddress.ip_address("64:ff9b::203.0.113.9") in ipaddress.ip_network(
            "64:ff9b::cb00:7100/120"
        )
        for net in internet["0.0.0.0/0"]:
            assert _covered(_nat64(net), v6), net


@pytest.mark.parametrize(
    "sets",
    [
        {"executions.channel.ipv6": "true"},
        {"network.listenAddress": "::"},
    ],
)
def test_an_ipv6_cluster_needs_the_vpc_ipv6_block(sets: dict[str, str]) -> None:
    """Num cluster IPv6, os pods e os serviços têm endereços globais: sem o bloco da VPC nas
    redes internas, a regra da internet (::/0) abriria o OpenBao, o Keycloak e o S3."""
    for extra in ([], ["10.200.0.0/16"]):
        error = template_error(
            "values-producao-exemplo.yaml",
            sets=sets,
            set_json={"executions.egress.extraInternalNetworks": extra},
        )
        assert "extraInternalNetworks" in error and "IPv6" in error
    assert template("values-producao-exemplo.yaml", sets=sets).docs


def test_run_pods_reach_only_the_dispatcher_channel_inside_the_cluster() -> None:
    spec = _policies(rendered("producao"), "rapa-execucoes")["rapa-executor"]
    selected = [rule for rule in spec["egress"] if any("podSelector" in p for p in rule["to"])]
    # Dentro do cluster, só o DNS e o canal do despachante.
    assert len(selected) == 2
    dns, channel = selected
    assert dns["to"][0]["podSelector"] == {"matchLabels": {"k8s-app": "kube-dns"}}
    assert {p["port"] for p in dns["ports"]} == {53}
    assert channel["ports"] == [{"protocol": "TCP", "port": 8081}]
    labels = channel["to"][0]["podSelector"]["matchLabels"]
    assert labels["app.kubernetes.io/component"] == "despachante"
    assert channel["to"][0]["namespaceSelector"] == {
        "matchLabels": {"kubernetes.io/metadata.name": "rapa"}
    }
    # A sessão nunca alcança o canal.
    session = _policies(rendered("producao"), "rapa-execucoes")["rapa-sessao"]
    assert 8081 not in _egress_ports(session)


def test_internet_only_through_the_proxy() -> None:
    render = template(
        "values-producao-exemplo.yaml", sets={"executions.egress.directInternet": "false"}
    )
    spec = _policies(render, "rapa-execucoes")["rapa-executor"]
    with pytest.raises(AssertionError, match="sem a regra da internet"):
        _internet_rule(spec)
    error = template_error(
        "values-producao-exemplo.yaml",
        sets={"executions.egress.directInternet": "false", "executions.egress.proxy.url": ""},
    )
    assert "proxy" in error


#: CIDRs que nunca abrem para os robôs: a internet inteira (também escrita com zeros à
#: esquerda, que o Python e o Kubernetes leem como /0), metades dela, redes largas, o
#: loopback, o link-local e o metadata da nuvem (também em IPv6 e pelo NAT64), o 6to4, o
#: IPv4 escrito dentro do IPv6, bits de host e o que não é CIDR.
WIDE_OR_FORBIDDEN = [
    "0.0.0.0/0",
    "::/0",
    "0.0.0.0/00",
    "::/000",
    "10.30.0.0/024",
    "010.30.0.0/24",
    "0.0.0.0/1",
    "128.0.0.0/1",
    "::/1",
    "10.0.0.0/8",
    "2001:db8::/32",
    "169.254.169.254/32",
    "169.254.0.0/16",
    "127.0.0.1/32",
    "0.0.0.0/8",
    "224.0.0.0/4",
    "fd00:ec2::254/128",
    "fd00:ec2::/32",
    "::1/128",
    "::ffff:a9fe:a9fe/128",
    "::ffff:169.254.169.254/128",
    "64:ff9b::a9fe:a9fe/128",
    "2002:a9fe:a9fe::/48",
    "fe80::/64",
    "10.30.0.1/24",
    "rede-interna",
    "10.0.0.0",
    "",
]


@pytest.mark.parametrize("key", ["allowedInternalNetworks", "proxy.cidrs"])
@pytest.mark.parametrize("network", WIDE_OR_FORBIDDEN)
def test_allowed_internal_networks_must_be_narrow_cidrs(key: str, network: str) -> None:
    """RAPA_REDES_INTERNAS_PERMITIDAS e o proxy: CIDRs canônicos e estreitos (/16 em IPv4,
    /48 em IPv6), longe das redes que nunca se abrem."""
    error = template_error(
        "values-producao-exemplo.yaml",
        set_json={f"executions.egress.{key}": [network]},
    )
    assert f"executions.egress.{key}" in error and f'"{network}"' in error


@pytest.mark.parametrize("network", ["10.30.0.0/16", "192.168.10.0/24", "fd12:3456:789a::/48"])
def test_narrow_internal_networks_are_accepted(network: str) -> None:
    render = template(
        "values-producao-exemplo.yaml",
        set_json={"executions.egress.allowedInternalNetworks": [network]},
    )
    assert _env_of(render, "despachante")["RAPA_REDES_INTERNAS_PERMITIDAS"] == network


@pytest.mark.parametrize("network", ["0.0.0.0/0", "::/0", "0.0.0.0/00", "10.0.0.1/8", "rede"])
def test_extra_internal_networks_must_be_cidrs(network: str) -> None:
    error = template_error(
        "values-producao-exemplo.yaml",
        set_json={"executions.egress.extraInternalNetworks": ["2001:db8::/32", network]},
    )
    assert "executions.egress.extraInternalNetworks" in error


def test_extra_egress_rules_only_in_dev() -> None:
    """Regras de saída a mais passariam por cima da validação e da guarda: só no kind."""
    peer = {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "x"}}}
    rule: dict[str, Any] = {"to": [peer], "ports": [{"protocol": "TCP", "port": 8080}]}
    error = template_error(
        "values-producao-exemplo.yaml", set_json={"executions.egress.extraRules": [rule]}
    )
    assert "extraRules" in error and "auth.dev.enabled" in error
    # No kind, só com destinos por namespace, sem ipBlock e com portas.
    for bad in (
        {"to": [{"ipBlock": {"cidr": "10.0.0.0/8"}}], "ports": rule["ports"]},
        {"to": [{**peer, "ipBlock": {"cidr": "10.0.0.0/8"}}], "ports": rule["ports"]},
        {"to": [{"namespaceSelector": {}}], "ports": rule["ports"]},
        {"to": [{"podSelector": {}}], "ports": rule["ports"]},
        {"to": [], "ports": rule["ports"]},
        {"to": rule["to"]},
        {"ports": rule["ports"]},
    ):
        error = template_error("values-kind.yaml", set_json={"executions.egress.extraRules": [bad]})
        assert "extraRules" in error, bad
    kind = _policies(rendered("kind"), "rapa-execucoes")["rapa-executor"]
    assert kind["egress"][-1]["to"][0]["namespaceSelector"] == {
        "matchLabels": {"kubernetes.io/metadata.name": "rapa-teste"}
    }


def test_proxy_needs_its_networks() -> None:
    error = template_error(
        "values-producao-exemplo.yaml", set_json={"executions.egress.proxy.cidrs": []}
    )
    assert "proxy.cidrs" in error


def test_allowed_internal_networks_reach_the_pods_and_the_policy() -> None:
    for component in ("web", "despachante"):
        env = _env("producao", component)
        assert env["RAPA_REDES_INTERNAS_PERMITIDAS"] == "10.30.0.0/24"
        assert env["RAPA_PROXY_SAIDA"] == "http://proxy.interno.exemplo.test:3128"


# ---------------------------------------------------------------------------------------------
# Gateway e rotas
# ---------------------------------------------------------------------------------------------


def _segments_prefix(prefix: str, path: str) -> bool:
    """PathPrefix do Gateway API: casa por segmentos (/v1/sessoes não casa /v1/sessoesx)."""
    if prefix == "/":
        return True
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")


def _rule(render: Render, path: str) -> dict[str, Any]:
    """A regra que o Envoy Gateway escolhe para ``path``: o caminho exato, depois a expressão
    regular (casando o caminho inteiro) e por fim o prefixo mais longo."""
    best: tuple[tuple[int, int], dict[str, Any]] | None = None
    for route in render.of_kind("HTTPRoute"):
        for rule in route["spec"]["rules"]:
            for match in rule["matches"]:
                kind, value = match["path"]["type"], match["path"]["value"]
                if kind == "Exact":
                    rank = (3, len(value)) if path == value else None
                elif kind == "RegularExpression":
                    rank = (2, len(value)) if re.fullmatch(value, path) else None
                else:
                    assert kind == "PathPrefix", kind
                    rank = (1, len(value)) if _segments_prefix(value, path) else None
                if rank is not None and (best is None or rank > best[0]):
                    best = (rank, rule)
    assert best is not None, path
    return best[1]


def _route(render: Render, path: str) -> str:
    """O backend que o Gateway escolhe para ``path``."""
    name: str = _rule(render, path)["backendRefs"][0]["name"]
    return name


@pytest.mark.parametrize(
    ("path", "backend"),
    [
        ("/", "rapa-web"),
        ("/auth/oidc/retorno", "rapa-web"),
        ("/_nicegui_ws/socket.io/", "rapa-web"),
        ("/v1/sessoes", "rapa-web"),
        ("/v1/sessoes/019a/ao-vivo", "rapa-web"),
        ("/v1/sessoes/019a/propostas", "rapa-web"),
        ("/v1/sessoesx", "rapa-api"),
        ("/v1/execucoes/019a", "rapa-api"),
        ("/v1/webhooks/019a", "rapa-api"),
        ("/v1/openapi-robos.json", "rapa-api"),
        ("/mcp", "rapa-api"),
        ("/.well-known/oauth-protected-resource/mcp", "rapa-api"),
        ("/saude", "rapa-web"),
    ],
)
def test_routes_send_each_path_to_its_backend(path: str, backend: str) -> None:
    assert _route(rendered("producao"), path) == backend


@pytest.mark.parametrize(
    ("path", "timeout"),
    [
        # Sem prazo, só os websockets e as respostas em fluxo do MCP.
        ("/_nicegui_ws/socket.io/", "0s"),
        ("/v1/sessoes/019a0000-0000-7000-8000-000000000000/ao-vivo", "0s"),
        ("/mcp", "0s"),
        # O resto (as páginas e os envios da interface, as sessões, a REST), com prazo.
        ("/", "180s"),
        ("/robos", "180s"),
        ("/_nicegui/client/1/upload/2", "180s"),
        ("/auth/oidc/retorno", "180s"),
        ("/v1/sessoes", "180s"),
        ("/v1/sessoes/019a/propostas", "180s"),
        ("/v1/sessoes/019a/ao-vivo/x", "180s"),
        ("/v1/sessoes/019a/x/ao-vivo", "180s"),
        ("/v1/execucoes", "180s"),
        ("/v1/webhooks/019a", "180s"),
        ("/.well-known/oauth-protected-resource/mcp", "180s"),
    ],
)
def test_only_websockets_and_streams_have_no_deadline(path: str, timeout: str) -> None:
    assert _rule(rendered("producao"), path)["timeouts"]["request"] == timeout
    render = template("values-producao-exemplo.yaml", sets={"gateway.requestTimeout": "45s"})
    expected = timeout if timeout == "0s" else "45s"
    assert _rule(render, path)["timeouts"]["request"] == expected


def test_routes_use_the_external_gateway() -> None:
    render = rendered("producao")
    for route in render.of_kind("HTTPRoute"):
        spec = route["spec"]
        assert spec["parentRefs"] == [
            {"name": "rapa-externo", "namespace": "envoy-gateway-system", "sectionName": "https"}
        ]
        assert spec["hostnames"] == ["rapa.exemplo.test"]
    # Sem gateway, sem rotas.
    assert rendered("kind").of_kind("HTTPRoute") == []


def test_web_affinity_only_with_more_than_one_replica() -> None:
    policies = rendered("producao").of_kind("BackendTrafficPolicy")
    assert len(policies) == 1
    spec = policies[0]["spec"]
    assert spec["targetRefs"] == [
        {"group": "gateway.networking.k8s.io", "kind": "HTTPRoute", "name": "rapa-web"}
    ]
    assert spec["loadBalancer"]["consistentHash"]["type"] == "Cookie"
    single = template("values-producao-exemplo.yaml", sets={"web.replicas": "1"})
    assert single.of_kind("BackendTrafficPolicy") == []


def test_forwarded_allow_ips_is_the_gateway_network() -> None:
    for component in ("web", "api"):
        assert _env("producao", component)["FORWARDED_ALLOW_IPS"] == "10.0.32.0/20"
    # Sem as redes do gateway, o uvicorn fica com o padrão dele (só 127.0.0.1).
    assert "FORWARDED_ALLOW_IPS" not in _env("kind", "web")
    assert "FORWARDED_ALLOW_IPS" not in _env("producao", "despachante")


@pytest.mark.parametrize(
    "networks",
    [
        ["*"],
        ["0.0.0.0/0"],
        ["::/0"],
        ["0.0.0.0/00"],
        ["::/000"],
        ["0.0.0.0/1", "128.0.0.0/1"],
        ["::/1", "8000::/1"],
        ["10.0.0.0/8,*"],
        [""],
        ["8.8.8.0/24"],
        ["2001:db8::/32"],
        ["::1/128"],
        ["10.0.32.1/20"],
    ],
)
def test_forwarded_allow_ips_is_never_everyone(networks: list[str]) -> None:
    """Só as redes dos pods do Envoy: IPv4 dentro das redes privadas dos pods, IPv6 /48."""
    error = template_error(
        "values-producao-exemplo.yaml",
        set_json={"gateway.trustedNetworks": ["10.0.32.0/20", *networks]},
    )
    assert "FORWARDED_ALLOW_IPS" in error


def test_the_gateway_needs_the_envoy_networks() -> None:
    """Sem FORWARDED_ALLOW_IPS, o uvicorn vê o Envoy como origem de todo pedido."""
    error = template_error("values-producao-exemplo.yaml", set_json={"gateway.trustedNetworks": []})
    assert "gateway.trustedNetworks" in error
    render = template(
        "values-producao-exemplo.yaml",
        set_json={"gateway.trustedNetworks": ["100.64.0.0/16", "2001:db8:1::/48"]},
    )
    assert _env_of(render, "api")["FORWARDED_ALLOW_IPS"] == "100.64.0.0/16,2001:db8:1::/48"


def test_gateway_needs_hosts_public_url_and_keycloak() -> None:
    assert "hostnames" in template_error(
        "values-producao-exemplo.yaml", set_json={"gateway.hostnames": []}
    )
    assert "publicUrl" in template_error("values-producao-exemplo.yaml", sets={"publicUrl": ""})
    assert "auth.jwt" in template_error(
        "values-producao-exemplo.yaml", sets={"auth.jwt.issuer": ""}
    )


# ---------------------------------------------------------------------------------------------
# Componentes
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("variant", APP_VARIANTS)
def test_components_and_their_modes(variant: str) -> None:
    web, api, dispatcher = (_env(variant, c) for c in COMPONENTS)
    assert (web["RAPA_HTTP"], web["RAPA_UI"], web["RAPA_SESSOES"]) == ("1", "1", "1")
    assert (api["RAPA_HTTP"], api["RAPA_UI"], api["RAPA_SESSOES"]) == ("1", "0", "0")
    assert dispatcher["RAPA_HTTP"] == "0" and dispatcher["RAPA_SESSOES"] == "0"
    # Só o despachante despacha (a URL do papel dele liga o despachante no processo).
    assert "RAPA_DISPATCHER_DATABASE_URL" in dispatcher
    assert "RAPA_DISPATCHER_DATABASE_URL" not in web and "RAPA_DISPATCHER_DATABASE_URL" not in api
    assert dispatcher["RAPA_EXECUTOR"] == "kubernetes" and "RAPA_EXECUTOR" not in web
    assert web["RAPA_SESSOES_EXECUTOR"] == "kubernetes"
    # Só o despachante lê o cofre; a web e a api usam o OpenBao só para as chaves.
    assert web["RAPA_COFRE"] == api["RAPA_COFRE"] == "nenhum"
    assert dispatcher["RAPA_COFRE"] == "openbao"
    assert (web["RAPA_BAO_PAPEL"], api["RAPA_BAO_PAPEL"], dispatcher["RAPA_BAO_PAPEL"]) == (
        "rapa-web",
        "rapa-api",
        "rapa-despachante",
    )
    replicas = {
        c: rendered(variant).get("Deployment", f"rapa-{c}")["spec"].get("replicas")
        for c in COMPONENTS
    }
    if variant == "padrao":
        assert replicas == {"web": 1, "api": 2, "despachante": 2}


def test_dispatcher_health_and_channel_have_their_own_ports() -> None:
    """A saúde do despachante (/saude e /saude/pronto, o --port do serviço) numa porta e o
    canal dos pods (o servidor próprio dele, com os limites do canal) noutra."""
    pod = deployment_pod(rendered("producao"), "rapa-despachante")
    container = pod["containers"][0]
    assert container["args"][-2:] == ["--port", "8080"]
    assert {p["name"]: p["containerPort"] for p in container["ports"]} == {
        "saude": 8080,
        "canal": 8081,
        "metricas": 9100,
    }
    for probe, path in (
        ("startupProbe", "/saude"),
        ("livenessProbe", "/saude"),
        ("readinessProbe", "/saude/pronto"),
    ):
        assert container[probe]["httpGet"] == {"path": path, "port": "saude"}
    env = container_env(pod)
    assert env["RAPA_CANAL_PORTA"] == "8081"
    # O canal desta réplica: {ip} vira o IP do próprio pod no RaPA (os bilhetes ficam na
    # memória dela); um endereço sem {ip} seria recusado na partida.
    assert env["RAPA_CANAL_ENDERECO"] == "http://{ip}:8081"
    assert "RAPA_POD_IP" not in env
    # Nenhum Service espalha o canal entre as réplicas.
    for service in rendered("producao").of_kind("Service"):
        assert service["spec"]["selector"].get("app.kubernetes.io/component") != "despachante"
    ipv6 = template("values-producao-exemplo.yaml", sets={"executions.channel.ipv6": "true"})
    env6 = container_env(deployment_pod(ipv6, "rapa-despachante"))
    # O RaPA põe os colchetes de um IPv6.
    assert env6["RAPA_CANAL_ENDERECO"] == "http://{ip}:8081"


def test_the_dispatcher_ports_never_cross() -> None:
    for sets in (
        {"dispatcher.healthPort": "8081"},
        {"observability.metricsPort": "8081"},
        {"observability.metricsPort": "8080"},
        {"observability.metricsPort": "8000"},
    ):
        assert "porta" in template_error("values-producao-exemplo.yaml", sets=sets)
    error = template_error("values-producao-exemplo.yaml", sets={"executions.sessionPort": "9000"})
    assert "8765" in error


@pytest.mark.parametrize("component", ["web", "api"])
def test_http_probes(component: str) -> None:
    container = deployment_pod(rendered("producao"), f"rapa-{component}")["containers"][0]
    assert container["args"][-4:] == ["--host", "0.0.0.0", "--port", "8000"]
    assert container["livenessProbe"]["httpGet"] == {"path": "/saude", "port": "http"}
    assert container["readinessProbe"]["httpGet"] == {"path": "/saude/pronto", "port": "http"}


def test_images_come_from_the_values() -> None:
    render = rendered("producao")
    control = "registro.exemplo.test/rapa/rapa-controle@sha256:" + "1" * 64
    executor = "registro.exemplo.test/rapa/rapa-executor@sha256:" + "2" * 64
    for owner, pod in render.pod_specs():
        assert {c["image"] for c in pod["containers"]} == {control}, owner
        assert pod["imagePullSecrets"] == [{"name": "rapa-registro"}], owner
    for component in ("web", "despachante"):
        env = _env("producao", component)
        assert env["RAPA_K8S_IMAGEM_EXECUTOR"] == executor
        assert env["RAPA_K8S_PULL_SECRET"] == "rapa-registro"
    assert _env("padrao", "web")["RAPA_K8S_IMAGEM_EXECUTOR"] == (
        "registro.exemplo.test/rapa/rapa-executor:0.1.0"
    )


def test_production_requires_image_digests() -> None:
    error = template_error("values-producao-exemplo.yaml", sets={"images.executor.digest": ""})
    assert "images.requireDigest" in error
    error = template_error("values-producao-exemplo.yaml", sets={"images.control.digest": "abc"})
    assert "digest inválido" in error


def test_pods_of_single_use_settings() -> None:
    for component in ("web", "despachante"):
        env = _env("producao", component)
        assert env["RAPA_K8S_NAMESPACE_EXECUCOES"] == "rapa-execucoes"
        assert env["RAPA_K8S_RUNTIME_CLASS"] == "gvisor"
        assert (env["RAPA_K8S_CPU"], env["RAPA_K8S_MEMORIA"]) == ("1", "2Gi")
        assert env["RAPA_K8S_SA_EXECUTOR"] == "rapa-executor"
        assert env["RAPA_K8S_NODE_SELECTOR"] == '{"rapa.dev/execucoes":"sim"}'
        assert env["RAPA_K8S_TOLERATIONS"] == (
            '[{"effect":"NoSchedule","key":"rapa.dev/execucoes","operator":"Exists"}]'
        )
    # No kind, sem gVisor.
    assert _env("kind", "despachante")["RAPA_K8S_RUNTIME_CLASS"] == ""


def test_no_gvisor_only_in_dev() -> None:
    error = template_error("values-producao-exemplo.yaml", sets={"executions.runtimeClass": ""})
    assert "gVisor" in error


def test_pdb_spread_and_autoscaling() -> None:
    render = rendered("producao")
    budgets = {p["metadata"]["name"]: p["spec"] for p in render.of_kind("PodDisruptionBudget")}
    for component in COMPONENTS:
        name = f"rapa-{component}"
        assert budgets[name]["maxUnavailable"] == 1
        selector = render.get("Deployment", name)["spec"]["selector"]
        assert budgets[name]["selector"] == selector
        pod = deployment_pod(render, name)
        keys = {c["topologyKey"] for c in pod["topologySpreadConstraints"]}
        assert keys == {"kubernetes.io/hostname", "topology.kubernetes.io/zone"}
    hpa = render.get("HorizontalPodAutoscaler", "rapa-api")["spec"]
    assert hpa["scaleTargetRef"]["name"] == "rapa-api"
    assert "replicas" not in render.get("Deployment", "rapa-api")["spec"]
    assert rendered("padrao").of_kind("HorizontalPodAutoscaler") == []


def test_dispatcher_waits_for_runs_on_shutdown() -> None:
    pod = deployment_pod(rendered("padrao"), "rapa-despachante")
    # O prazo máximo de uma execução (30 min) mais 10 minutos.
    assert pod["terminationGracePeriodSeconds"] == 2400
    volumes = {v["name"]: v for v in pod["volumes"]}
    assert volumes["trabalho"]["emptyDir"] == {"sizeLimit": "20Gi"}
    env = container_env(pod)
    assert (env["RAPA_PASTA_TRABALHO"], env["RAPA_PASTA_ARTEFATOS"]) == (
        "/trabalho/execucoes",
        "/trabalho/artefatos",
    )


def test_openbao_token_is_projected_with_its_own_audience() -> None:
    for component in COMPONENTS:
        pod = deployment_pod(rendered("producao"), f"rapa-{component}")
        volume = next(v for v in pod["volumes"] if v["name"] == "bao-token")
        source = volume["projected"]["sources"][0]["serviceAccountToken"]
        assert source == {"audience": "openbao", "expirationSeconds": 3600, "path": "token"}
        assert container_env(pod)["RAPA_BAO_TOKEN_SA"] == "/var/run/secrets/rapa/bao/token"
    # Sem o OpenBao (chaves locais e cofre de dev), nenhum token projetado.
    render = template(
        "values-kind.yaml",
        sets={
            "keys.mode": "local",
            "keys.localKeySecret.name": "rapa-chave-artefatos",
            "vault.mode": "dev",
            "auth.dev.secretsSecret": "rapa-dev-segredos",
        },
    )
    for component in COMPONENTS:
        pod = deployment_pod(render, f"rapa-{component}")
        assert all(v["name"] != "bao-token" for v in pod["volumes"])
        assert "RAPA_BAO_ENDERECO" not in container_env(pod)
    dispatcher = container_env(deployment_pod(render, "rapa-despachante"))
    assert dispatcher["RAPA_COFRE"] == "dev"
    assert dispatcher["RAPA_SEGREDOS_DEV"] == "/var/run/rapa/dev/segredos/segredos.json"
    assert dispatcher["RAPA_CHAVE_ARTEFATOS"]["secretKeyRef"]["name"] == "rapa-chave-artefatos"
    # Os segredos de dev só no despachante; os tokens de dev só na web e na api.
    assert "RAPA_SEGREDOS_DEV" not in container_env(deployment_pod(render, "rapa-web"))
    assert "RAPA_TOKENS_DEV" not in dispatcher


def test_dev_mode_is_never_behind_the_public_gateway() -> None:
    error = template_error(
        "values-producao-exemplo.yaml",
        sets={"auth.dev.enabled": "true", "auth.dev.tokensSecret": "rapa-dev-tokens"},
    )
    assert "auth.dev.enabled" in error and "gateway" in error


def test_dev_vault_needs_dev_mode() -> None:
    error = template_error("values-producao-exemplo.yaml", sets={"vault.mode": "dev"})
    assert "vault.mode=dev" in error


def test_local_storage_is_refused_in_the_cluster() -> None:
    error = template_error(sets={"storage.mode": "local"})
    assert "storage.mode=local" in error


@pytest.mark.parametrize(
    ("key", "value"),
    [("vault.mode", "vault"), ("storage.mode", "nfs"), ("keys.mode", "kms")],
)
def test_unknown_modes_are_refused(key: str, value: str) -> None:
    assert key in template_error(sets={key: value})


def test_kind_values_are_for_kind() -> None:
    web = _env("kind", "web")
    assert web["RAPA_DEV"] == "1"
    assert web["RAPA_TOKENS_DEV"] == "/var/run/rapa/dev/tokens/tokens.json"
    assert rendered("kind").of_kind("HTTPRoute") == []
    assert _env("producao", "web")["RAPA_DEV"] == "0"
    assert "RAPA_TOKENS_DEV" not in _env("producao", "web")


# ---------------------------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------------------------


def test_rbac_only_in_the_executions_namespace() -> None:
    render = rendered("producao")
    assert render.of_kind("ClusterRole") == [] and render.of_kind("ClusterRoleBinding") == []
    role = render.get("Role", "rapa-pods-de-uso-unico", "rapa-execucoes")
    assert role["rules"] == [
        {
            "apiGroups": [""],
            "resources": ["pods", "secrets"],
            "verbs": ["create", "get", "list", "watch", "delete"],
        },
        {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
    ]
    bindings = render.of_kind("RoleBinding")
    assert len(bindings) == 1
    subjects = {(s["namespace"], s["name"]) for s in bindings[0]["subjects"]}
    # A api e a conta dos pods de execução não têm permissão nenhuma.
    assert subjects == {("rapa", "rapa-despachante"), ("rapa", "rapa-web")}


def _admission(render: Render) -> dict[str, Any]:
    policy = render.get("ValidatingAdmissionPolicy", "rapa-execucoes-pods-de-uso-unico")
    binding = render.get("ValidatingAdmissionPolicyBinding", "rapa-execucoes-pods-de-uso-unico")
    assert binding["spec"] == {
        "policyName": "rapa-execucoes-pods-de-uso-unico",
        "validationActions": ["Deny"],
    }
    spec: dict[str, Any] = policy["spec"]
    return spec


@pytest.mark.parametrize("variant", APP_VARIANTS)
def test_accounts_that_create_pods_only_create_single_use_pods(variant: str) -> None:
    """O token da web ou do despachante só cria, no namespace das execuções, o pod de uso
    único como o lançador o monta: a imagem do executor, a conta sem token, o gVisor, o
    rótulo e o comando de execução ou de sessão, e só volumes secret e emptyDir."""
    spec = _admission(rendered(variant))
    assert spec["failurePolicy"] == "Fail"
    constraints = spec["matchConstraints"]
    assert constraints["namespaceSelector"] == {
        "matchLabels": {"kubernetes.io/metadata.name": "rapa-execucoes"}
    }
    assert constraints["resourceRules"] == [
        {
            "apiGroups": [""],
            "apiVersions": ["v1"],
            "operations": ["CREATE", "UPDATE"],
            "resources": ["pods"],
        }
    ]
    # As contas da Role rapa-pods-de-uso-unico, e só elas.
    [condition] = spec["matchConditions"]
    assert condition["expression"] == (
        "request.userInfo.username in ['system:serviceaccount:rapa:rapa-despachante', "
        "'system:serviceaccount:rapa:rapa-web']"
    )
    rules = "\n".join(v["expression"] for v in spec["validations"])
    image = _env(variant, "despachante")["RAPA_K8S_IMAGEM_EXECUTOR"]
    assert f"c.image == '{image}'" in rules
    assert "object.spec.serviceAccountName == 'rapa-executor'" in rules
    assert "object.spec.automountServiceAccountToken == false" in rules
    assert "variables.app in ['rapa-executor', 'rapa-sessao']" in rules
    assert "'rapa.session.host' : 'rapa.runner.pod'" in rules
    assert "has(v.secret) || has(v.emptyDir)" in rules
    assert "!has(c.command)" in rules and "PYTHON" in rules
    gvisor = "object.spec.runtimeClassName == 'gvisor'"
    # No kind (sem gVisor), a RuntimeClass não é exigida; fora dele, sempre.
    assert (gvisor in rules) is (variant != "kind")


def test_the_admission_policy_is_never_off_outside_dev() -> None:
    error = template_error(
        "values-producao-exemplo.yaml", sets={"executions.admissionPolicy.enabled": "false"}
    )
    assert "executions.admissionPolicy.enabled=false" in error
    render = template("values-kind.yaml", sets={"executions.admissionPolicy.enabled": "false"})
    assert render.of_kind("ValidatingAdmissionPolicy") == []


# ---------------------------------------------------------------------------------------------
# Migração
# ---------------------------------------------------------------------------------------------


def test_migration_job_runs_as_the_table_owner_before_install_and_upgrade() -> None:
    render = rendered("producao")
    job = render.get("Job", "rapa-migracao")
    annotations = job["metadata"]["annotations"]
    assert annotations["helm.sh/hook"] == "pre-install,pre-upgrade"
    assert annotations["helm.sh/hook-delete-policy"] == "before-hook-creation"
    pod = job["spec"]["template"]["spec"]
    assert pod["restartPolicy"] == "Never"
    container = pod["containers"][0]
    assert container["args"] == ["python", "-m", "rapa.control.db.migrate"]
    env = container_env(pod)
    assert env["RAPA_DATABASE_URL"].startswith("postgresql://rapa_owner:$(BANCO_SENHA_OWNER)@")
    assert env["BANCO_SENHA_OWNER"]["secretKeyRef"]["name"] == "rapa-banco-dono"
    assert (env["RAPA_DB_APP_ROLE"], env["RAPA_DB_DISPATCHER_ROLE"]) == (
        "rapa_app",
        "rapa_dispatcher",
    )
    grants = render.get("Job", "rapa-permissoes")
    assert grants["metadata"]["annotations"]["helm.sh/hook"] == "post-install,post-upgrade"
    assert grants["spec"]["template"]["spec"]["containers"][0]["args"][-1] == (
        "--somente-permissoes"
    )
    assert "rapa-permissoes" not in rendered("padrao").names("Job")
    # Só a migração recebe a senha do dono.
    for component in COMPONENTS:
        assert "BANCO_SENHA_OWNER" not in _env("producao", component)


# ---------------------------------------------------------------------------------------------
# Operação (o CLI no cluster)
# ---------------------------------------------------------------------------------------------


def test_operation_pod_template_has_the_operation_identity() -> None:
    """Congelar, exportar e apagar uma empresa rodam num pod próprio (criado do PodTemplate):
    o papel rapa-operacao do OpenBao (apagar os segredos e a chave da empresa), a URL do dono
    das tabelas, o S3 e um disco próprio para a exportação, longe da api que serve tráfego."""
    render = rendered("producao")
    template_doc = render.get("PodTemplate", "rapa-operacao")
    metadata = template_doc["template"]["metadata"]
    # Sem app.kubernetes.io/instance: o Argo CD não toma o pod como recurso da Application.
    assert metadata["labels"] == {
        "app.kubernetes.io/name": "rapa",
        "app.kubernetes.io/part-of": "rapa",
        "app.kubernetes.io/component": "operacao",
    }
    pod = template_doc["template"]["spec"]
    assert pod["serviceAccountName"] == "rapa-operacao"
    assert pod["automountServiceAccountToken"] is False
    assert pod["restartPolicy"] == "Never" and pod["activeDeadlineSeconds"] == 14400
    container = pod["containers"][0]
    assert container["args"] == ["sleep", "infinity"]
    env = container_env(pod)
    assert env["RAPA_OWNER_DATABASE_URL"].startswith(
        "postgresql://rapa_owner:$(BANCO_SENHA_OWNER)@rapa-banco-pooler-rw:5432/rapa?"
        "sslmode=verify-full"
    )
    assert env["BANCO_SENHA_OWNER"]["secretKeyRef"]["name"] == "rapa-banco-dono"
    assert (env["RAPA_COFRE"], env["RAPA_CHAVES"], env["RAPA_ARMAZENAMENTO"]) == (
        "openbao",
        "openbao",
        "s3",
    )
    assert env["RAPA_BAO_PAPEL"] == "rapa-operacao"
    assert env["RAPA_PASTA_ARTEFATOS"] == "/trabalho/artefatos"
    volumes = {v["name"]: v for v in pod["volumes"]}
    assert volumes["trabalho"]["emptyDir"] == {"sizeLimit": "50Gi"}
    token = volumes["bao-token"]["projected"]["sources"][0]["serviceAccountToken"]
    assert token["audience"] == "openbao"
    # Nenhum outro componente recebe o papel da operação.
    for component in COMPONENTS:
        assert _env("producao", component)["RAPA_BAO_PAPEL"] != "rapa-operacao"


def test_operation_pod_only_reaches_the_database_s3_and_openbao() -> None:
    policy = _policies(rendered("producao"), "rapa")["rapa-operacao"]
    assert policy["podSelector"] == {
        "matchLabels": {
            "app.kubernetes.io/name": "rapa",
            "app.kubernetes.io/part-of": "rapa",
            "app.kubernetes.io/component": "operacao",
        }
    }
    assert policy["policyTypes"] == ["Ingress", "Egress"] and "ingress" not in policy
    assert _egress_ports(policy) == {53, 5432, 8333, 8200}


def test_database_needs_its_own_release() -> None:
    error = template_error("values-kind.yaml", sets={"database.cnpg.enabled": "true"})
    assert "release própria" in error


# ---------------------------------------------------------------------------------------------
# Banco (CloudNativePG)
# ---------------------------------------------------------------------------------------------


def test_database_release_has_only_the_database() -> None:
    render = rendered("banco-producao")
    kinds = {d["kind"] for d in render.docs}
    assert kinds == {
        "Cluster",
        "ObjectStore",
        "ScheduledBackup",
        "Pooler",
        "VMPodScrape",
        "NetworkPolicy",
    }
    # O nega-tudo do namespace é da release da aplicação (um recurso, uma release).
    assert "rapa-nega-tudo" not in render.names("NetworkPolicy")


def test_cloudnativepg_cluster_roles_and_backups() -> None:
    render = rendered("banco-producao")
    cluster = render.get("Cluster", "rapa-banco")["spec"]
    assert cluster["instances"] == 3
    assert cluster["enableSuperuserAccess"] is False
    initdb = cluster["bootstrap"]["initdb"]
    assert (initdb["database"], initdb["owner"], initdb["secret"]) == (
        "rapa",
        "rapa_owner",
        {"name": "rapa-banco-dono"},
    )
    roles = {r["name"]: r for r in cluster["managed"]["roles"]}
    assert roles["rapa_app"]["bypassrls"] is False
    assert roles["rapa_dispatcher"]["bypassrls"] is True
    for role, secret in (
        ("rapa_app", "rapa-banco-app"),
        ("rapa_dispatcher", "rapa-banco-despachante"),
    ):
        assert roles[role]["passwordSecret"] == {"name": secret}
        assert roles[role]["superuser"] is False and roles[role]["login"] is True
    assert cluster["plugins"] == [
        {
            "name": "barman-cloud.cloudnative-pg.io",
            "isWALArchiver": True,
            "parameters": {"barmanObjectName": "rapa-banco-backup"},
        }
    ]
    store = render.get("ObjectStore", "rapa-banco-backup")["spec"]
    assert store["retentionPolicy"] == "30d"
    assert store["configuration"]["endpointCA"] == {"name": "rapa-s3-ca", "key": "ca.crt"}
    backup = render.get("ScheduledBackup", "rapa-banco-diario")["spec"]
    assert (backup["schedule"], backup["method"]) == ("0 0 3 * * *", "plugin")
    pooler = render.get("Pooler", "rapa-banco-pooler-rw")["spec"]
    assert pooler["pgbouncer"]["poolMode"] == "session"
    # O Pooler serve com o certificado do banco: o nome dele está no certificado.
    assert "rapa-banco-pooler-rw" in cluster["certificates"]["serverAltDNSNames"]
    assert render.get("VMPodScrape", "rapa-banco")["spec"]["podMetricsEndpoints"] == [
        {"port": "metrics"}
    ]
    # O servidor recusa toda conexão por TCP sem TLS (antes da regra padrão, host).
    assert cluster["postgresql"]["pg_hba"] == ["hostnossl all all all reject"]
    assert "recovery" not in cluster["bootstrap"] and "externalClusters" not in cluster


RECOVERY = {
    "database.cnpg.name": "rapa-banco-2",
    "database.cnpg.recovery.enabled": "true",
    "database.cnpg.recovery.source": "rapa-banco",
}


def test_restoring_the_database_creates_a_new_cluster_from_the_backups() -> None:
    """A restauração: um Cluster de outro nome, nascido dos backups da origem com o mesmo dono
    (rapa_owner, que é quem está nos backups), com os papéis e o WAL na pasta dele."""
    render = template(
        "values-producao-exemplo.yaml",
        "values-banco.yaml",
        sets={**RECOVERY, "database.cnpg.recovery.targetTime": "2026-11-20T03:00:00Z"},
    )
    cluster = render.get("Cluster", "rapa-banco-2")["spec"]
    assert "initdb" not in cluster["bootstrap"]
    assert cluster["bootstrap"]["recovery"] == {
        "source": "origem",
        "database": "rapa",
        "owner": "rapa_owner",
        "secret": {"name": "rapa-banco-dono"},
        "recoveryTarget": {"targetTime": "2026-11-20T03:00:00Z"},
    }
    assert cluster["externalClusters"] == [
        {
            "name": "origem",
            "plugin": {
                "name": "barman-cloud.cloudnative-pg.io",
                "parameters": {
                    "barmanObjectName": "rapa-banco-2-origem",
                    "serverName": "rapa-banco",
                },
            },
        }
    ]
    assert {r["name"] for r in cluster["managed"]["roles"]} == {"rapa_app", "rapa_dispatcher"}
    # O WAL do novo vai para o ObjectStore dele (o nome do servidor é o do Cluster novo).
    assert cluster["plugins"][0]["parameters"] == {"barmanObjectName": "rapa-banco-2-backup"}
    stores = {s["metadata"]["name"]: s["spec"] for s in render.of_kind("ObjectStore")}
    assert set(stores) == {"rapa-banco-2-backup", "rapa-banco-2-origem"}
    assert (
        stores["rapa-banco-2-origem"]["configuration"]
        == stores["rapa-banco-2-backup"]["configuration"]
    )
    assert "retentionPolicy" not in stores["rapa-banco-2-origem"]
    # A NetworkPolicy do banco acompanha o nome (e o S3, para ler os backups).
    policy = _policies(render, "rapa")["rapa-banco-2"]
    assert policy["podSelector"] == {"matchLabels": {"cnpg.io/cluster": "rapa-banco-2"}}
    assert 8333 in _egress_ports(policy)
    assert kubeconform(render.text) == []


def test_the_restored_cluster_never_reuses_the_source_name() -> None:
    for sets in (
        {**RECOVERY, "database.cnpg.name": "rapa-banco"},
        {**RECOVERY, "database.cnpg.recovery.source": ""},
    ):
        error = template_error("values-producao-exemplo.yaml", "values-banco.yaml", sets=sets)
        assert "database.cnpg.recovery" in error


def test_the_application_reaches_the_cluster_by_its_name() -> None:
    """As NetworkPolicies da aplicação acham o banco pelo nome do Cluster (database.cnpg.name):
    trocar o Cluster, como na restauração, troca a saída de todos os componentes."""
    render = template("values-producao-exemplo.yaml", sets={"database.cnpg.name": "rapa-banco-2"})
    for name in ("rapa-web", "rapa-api", "rapa-despachante", "rapa-migracao", "rapa-operacao"):
        egress = _policies(render, "rapa")[name]["egress"]
        database = next(r for r in egress if r["ports"] == [{"protocol": "TCP", "port": 5432}])
        assert database["to"] == [
            {"podSelector": {"matchLabels": {"cnpg.io/cluster": "rapa-banco-2"}}},
            {"podSelector": {"matchLabels": {"cnpg.io/poolerName": "rapa-banco-2-pooler-rw"}}},
        ], name


def test_database_network_policies() -> None:
    policies = _policies(rendered("banco-producao"), "rapa")
    cluster = policies["rapa-banco"]
    clients = cluster["ingress"][0]
    assert clients["ports"] == [{"protocol": "TCP", "port": 5432}]
    assert {"podSelector": {"matchLabels": {"app.kubernetes.io/name": "rapa"}}} in clients["from"]
    assert {"podSelector": {"matchLabels": {"cnpg.io/poolerName": "rapa-banco-pooler-rw"}}} in (
        clients["from"]
    )
    assert 8333 in _egress_ports(cluster) and 443 in _egress_ports(cluster)
    assert _egress_ports(policies["rapa-banco-pooler-rw"]) == {53, 5432, 443}
    error = template_error(
        "values-kind.yaml", "values-banco.yaml", set_json={"network.egress.kubernetesApi.to": []}
    )
    assert "kubernetesApi" in error


# ---------------------------------------------------------------------------------------------
# Os nomes de configuração (contrato entre as partes)
# ---------------------------------------------------------------------------------------------

CONTRACT = {
    "RAPA_EXECUTOR": {"despachante"},
    "RAPA_K8S_NAMESPACE_EXECUCOES": {"web", "despachante"},
    "RAPA_K8S_IMAGEM_EXECUTOR": {"web", "despachante"},
    "RAPA_K8S_RUNTIME_CLASS": {"web", "despachante"},
    "RAPA_K8S_CPU": {"web", "despachante"},
    "RAPA_K8S_MEMORIA": {"web", "despachante"},
    "RAPA_K8S_SA_EXECUTOR": {"web", "despachante"},
    "RAPA_K8S_PULL_SECRET": {"web", "despachante"},
    "RAPA_K8S_NODE_SELECTOR": {"web", "despachante"},
    "RAPA_K8S_TOLERATIONS": {"web", "despachante"},
    "RAPA_CANAL_ENDERECO": {"despachante"},
    "RAPA_CANAL_PORTA": {"despachante"},
    "RAPA_PROXY_SAIDA": {"web", "despachante"},
    "RAPA_REDES_INTERNAS_PERMITIDAS": {"web", "despachante"},
    "RAPA_SESSOES_EXECUTOR": {"web"},
    "RAPA_COFRE": set(COMPONENTS),
    "RAPA_BAO_ENDERECO": set(COMPONENTS),
    "RAPA_BAO_CA": set(COMPONENTS),
    "RAPA_BAO_KV": set(COMPONENTS),
    "RAPA_BAO_TRANSIT": set(COMPONENTS),
    "RAPA_BAO_PAPEL": set(COMPONENTS),
    "RAPA_BAO_AUTH": set(COMPONENTS),
    "RAPA_BAO_TOKEN_SA": set(COMPONENTS),
    "RAPA_ARMAZENAMENTO": set(COMPONENTS),
    "RAPA_S3_ENDERECO": set(COMPONENTS),
    "RAPA_S3_REGIAO": set(COMPONENTS),
    "RAPA_S3_BUCKET": set(COMPONENTS),
    "RAPA_S3_CA": set(COMPONENTS),
    "AWS_ACCESS_KEY_ID": set(COMPONENTS),
    "AWS_SECRET_ACCESS_KEY": set(COMPONENTS),
    "RAPA_S3_ESTILO_CAMINHO": set(COMPONENTS),
    "RAPA_CHAVES": set(COMPONENTS),
    "RAPA_METRICAS_PORTA": set(COMPONENTS),
    "RAPA_LOG_JSON": set(COMPONENTS),
    "OTEL_EXPORTER_OTLP_ENDPOINT": set(COMPONENTS),
    "OTEL_SERVICE_NAME": set(COMPONENTS),
    "RAPA_GLITCHTIP_DSN": set(COMPONENTS),
}
#: Nomes do contrato que só valem fora do cluster (ou só com outro modo), conferidos à parte.
CONTRACT_ELSEWHERE = {"RAPA_SEGREDOS_DEV", "RAPA_CHAVE_ARTEFATOS", "RAPA_BAO_TOKEN_ARQUIVO"}


@pytest.mark.parametrize("name", sorted(CONTRACT))
def test_every_contract_name_reaches_its_components(name: str) -> None:
    """Cada nome do contrato chega exatamente aos componentes que o leem, e está nos valores."""
    present = {c for c in COMPONENTS if name in _env("producao", c)}
    assert present == CONTRACT[name]
    values = (CHART / "values.yaml").read_text(encoding="utf-8")
    assert name in values


def test_contract_names_outside_the_production_render() -> None:
    values = (CHART / "values.yaml").read_text(encoding="utf-8")
    for name in CONTRACT_ELSEWHERE:
        assert name in values
    # O token em arquivo do OpenBao é só para desenvolvimento fora do cluster.
    assert "RAPA_BAO_TOKEN_ARQUIVO" not in rendered("kind").text
    assert "RAPA_BAO_TOKEN_ARQUIVO" not in rendered("producao").text


def test_settings_reach_the_components_that_read_them() -> None:
    web, api, dispatcher = (_env("producao", c) for c in COMPONENTS)
    # O agendador roda no despachante; o resto da publicação, na web e na api.
    assert "RAPA_AGENDA_TOLERANCIA_S" in dispatcher and "RAPA_AGENDA_INTERVALO_S" in dispatcher
    assert "RAPA_LOTE_MAX_BYTES" in api and api["RAPA_LOTE_MAX_BYTES"] == "5242880"
    assert web["RAPA_CHAVE_WEBHOOKS"] == api["RAPA_CHAVE_WEBHOOKS"]
    assert "RAPA_CHAVE_WEBHOOKS" not in dispatcher
    assert dispatcher["RAPA_CONCORRENCIA"] == "4"
    # O login OIDC só na web; os tokens do Keycloak na web e na api.
    assert "RAPA_OIDC_CLIENT_ID" in web and "RAPA_OIDC_CLIENT_ID" not in api
    for env in (web, api):
        assert env["RAPA_JWT_ISSUER"] == "https://sso.exemplo.test/realms/rapa"
        assert env["RAPA_LLM_CAPACIDADES"] == "/var/run/rapa/hub/capacidades.json"
    assert "RAPA_JWT_ISSUER" not in dispatcher and "RAPA_LLM_BASE_URL" not in dispatcher
    # O nome de cada componente no OpenTelemetry.
    assert [e["OTEL_SERVICE_NAME"] for e in (web, api, dispatcher)] == [
        "rapa-web",
        "rapa-api",
        "rapa-despachante",
    ]


def test_hub_needs_the_probe_matrix() -> None:
    error = template_error("values-producao-exemplo.yaml", sets={"modelHub.capabilities": ""})
    assert "RAPA_LLM_CAPACIDADES" in error
