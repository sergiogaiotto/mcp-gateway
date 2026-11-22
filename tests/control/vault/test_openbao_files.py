"""As políticas e o preparo do OpenBao (``deploy/openbao``): o mínimo para cada papel, um
script idempotente sem segredo, e, se o binário ``bao`` existir, tudo contra ``bao server
-dev`` (o cliente, o KV, o transit e a destruição das chaves, de verdade)."""

import asyncio
import json
import os
import secrets
import shutil
import socket
import subprocess
import time
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx2
import pytest

from rapa.control.keys import KeyUnavailable, TransitCompanyKeys
from rapa.control.vault import (
    BaoReadiness,
    BaoSettings,
    OpenBaoClient,
    OpenBaoKeyDestroyer,
    VaultDenied,
)
from rapa.core.ids import new_id

from .acl import ACL, load_policy, parse_policy
from .fakebao import API_ROLE, OPERATION_ROLE, ROLE, WEB_ROLE, FakeBao
from .support import bao_client
from .tls import make_tls

ROOT = Path(__file__).resolve().parents[3]
FOLDER = ROOT / "deploy" / "openbao"
SCRIPT = FOLDER / "preparar.sh"
CUSTODIAN = FOLDER / "custodiante.sh"


def _policy(name: str) -> dict[str, tuple[set[str], dict[str, list[Any]] | None]]:
    # O mesmo leitor do OpenBao falso (recusa o que ele não sabe aplicar).
    return {
        path: (rule.capabilities, rule.allowed_parameters)
        for path, rule in load_policy(name).items()
    }


RENEW = {"auth/token/renew-self": ({"update"}, None)}


def test_each_role_gets_only_what_it_uses() -> None:
    # O despachante cria a chave da empresa pelo encrypt ("create" só vale ali, onde o
    # transit confere se a chave existe) e não tem nada em rapa-transit/keys/.
    assert _policy("rapa-despachante") == {
        "rapa/data/empresas/*": ({"read"}, None),
        "rapa-transit/encrypt/empresa-*": ({"create", "update"}, None),
        "rapa-transit/decrypt/empresa-*": ({"update"}, None),
        **RENEW,
    }
    assert _policy("rapa-api") == {"rapa-transit/decrypt/empresa-*": ({"update"}, None), **RENEW}
    # A operação só lê e apaga a chave, e na configuração só libera a exclusão.
    assert _policy("rapa-operacao") == {
        "rapa-transit/decrypt/empresa-*": ({"update"}, None),
        "rapa/metadata/empresas/*": ({"list", "delete"}, None),
        "rapa-transit/keys/+": ({"read", "delete"}, None),
        "rapa-transit/keys/+/config": ({"update"}, {"deletion_allowed": ["true"]}),
        **RENEW,
    }


async def _refused(client: OpenBaoClient, calls: list[tuple[str, str, Any]]) -> None:
    for method, path, body in calls:
        params = {"list": "true"} if method == "LIST" else None
        with pytest.raises(VaultDenied):
            await client.request(
                "GET" if method == "LIST" else method,
                path,
                json=body,
                params=params,
                what="pedido que a política recusa",
            )


KeyState = Callable[[str], Awaitable[dict[str, Any] | None]]


async def _each_role_does_its_work_and_nothing_more(
    dispatcher: OpenBaoClient,
    api: OpenBaoClient,
    operation: OpenBaoClient,
    key_state: KeyState,
    company: UUID,
    other: UUID,
) -> None:
    """O roteiro dos três papéis, contra o OpenBao falso e contra o bao server -dev.

    ``company`` e ``other`` já têm, cada uma, os segredos portal/acesso e fiscal/a1.
    """
    run, name = new_id(), f"empresa-{company}"
    data_key = os.urandom(32)
    # O despachante: o primeiro embrulho de uma empresa cria a chave dela, nunca
    # exportável; lê os segredos; e não mexe em chave nem em segredo nenhum.
    assert await key_state(name) is None
    wrapped = await TransitCompanyKeys(dispatcher).wrap(company, run, data_key)
    await TransitCompanyKeys(dispatcher).wrap(other, run, os.urandom(32))
    created = await key_state(name)
    assert created is not None
    assert (created["exportable"], created["allow_plaintext_backup"]) == (False, False)
    assert await TransitCompanyKeys(dispatcher).unwrap(company, run, wrapped) == data_key
    assert await dispatcher.read_kv("rapa", f"empresas/{company}/portal/acesso") == {
        "password": "senha-de-teste"
    }
    await _refused(
        dispatcher,
        [
            ("POST", f"rapa-transit/keys/{name}", {"exportable": True}),
            ("POST", f"rapa-transit/keys/{name}/config", {"exportable": True}),
            ("POST", f"rapa-transit/keys/{name}/rotate", {}),
            ("GET", f"rapa-transit/keys/{name}", None),
            ("DELETE", f"rapa-transit/keys/{name}", None),
            ("LIST", "rapa/metadata/empresas/", None),
            ("DELETE", f"rapa/metadata/empresas/{company}/portal/acesso", None),
            ("POST", f"rapa/data/empresas/{company}/novo", {"data": {"password": "x"}}),
        ],
    )
    # A API: só desembrulha; nem embrulha, nem cria a chave de uma empresa nova.
    assert await TransitCompanyKeys(api).unwrap(company, run, wrapped) == data_key
    newcomer = new_id()
    with pytest.raises(KeyUnavailable):
        await TransitCompanyKeys(api).wrap(newcomer, run, os.urandom(32))
    assert await key_state(f"empresa-{newcomer}") is None
    with pytest.raises(VaultDenied):
        await api.read_kv("rapa", f"empresas/{company}/portal/acesso")
    # A operação: desembrulha (exportar) e apaga; nunca lê um segredo, e da chave só
    # libera a exclusão: nem exportável, nem cópia em claro, nem girar ou cortar versões.
    assert await TransitCompanyKeys(operation).unwrap(company, run, wrapped) == data_key
    with pytest.raises(VaultDenied):
        await operation.read_kv("rapa", f"empresas/{company}/portal/acesso")
    config = f"rapa-transit/keys/{name}/config"
    await _refused(
        operation,
        [
            ("POST", config, {"exportable": "true"}),
            ("POST", config, {"exportable": True}),
            ("POST", config, {"allow_plaintext_backup": True}),
            ("POST", config, {"deletion_allowed": "true", "exportable": True}),
            ("POST", config, {"deletion_allowed": "false"}),
            ("POST", config, {"min_decryption_version": 1}),
            ("POST", f"rapa-transit/keys/{name}/rotate", {}),
            ("POST", f"rapa-transit/keys/{name}/trim", {"min_available_version": 1}),
            ("POST", f"rapa-transit/keys/empresa-{newcomer}", {}),
            ("POST", f"rapa-transit/encrypt/{name}", {"plaintext": "AAAA"}),
        ],
    )
    kept = await key_state(name)
    assert kept is not None
    assert (kept["exportable"], kept["allow_plaintext_backup"]) == (False, False)
    assert kept["latest_version"] == 1
    counts = await OpenBaoKeyDestroyer(operation).destroy(company)
    assert counts == {"segredos": 2, "chaves_transit": 1}
    assert await key_state(name) is None
    assert await key_state(f"empresa-{other}") is not None
    assert await dispatcher.read_kv("rapa", f"empresas/{company}/portal/acesso") is None
    assert await dispatcher.read_kv("rapa", f"empresas/{other}/portal/acesso") is not None


async def test_each_role_does_its_work_and_nothing_more(tmp_path: Path) -> None:
    """As três políticas de verdade, aplicadas pelo OpenBao falso, com a entrada de cada
    papel (sem as políticas certas, o despachante nunca cria a chave de uma empresa)."""
    bao = FakeBao()
    company, other = new_id(), new_id()
    for who in (company, other):
        for path in ("portal/acesso", "fiscal/a1"):
            bao.put_secret(f"empresas/{who}/{path}", {"password": "senha-de-teste"})

    async def key_state(name: str) -> dict[str, Any] | None:
        key = bao.keys.get(name)
        if key is None:
            return None
        return {
            "exportable": key.exportable,
            "allow_plaintext_backup": key.plaintext_backup,
            "latest_version": key.version,
        }

    clients = [bao_client(bao, tmp_path, role=role) for role in (ROLE, API_ROLE, OPERATION_ROLE)]
    try:
        dispatcher, api, operation = clients
        await _each_role_does_its_work_and_nothing_more(
            dispatcher, api, operation, key_state, company, other
        )
    finally:
        for client in clients:
            await client.aclose()
    assert {policies for _, _, policies in bao.refusals} == {
        (ROLE,),
        (API_ROLE,),
        (OPERATION_ROLE,),
    }


async def test_the_web_role_only_unwraps(tmp_path: Path) -> None:
    """A interface entra com o papel dela (rapa-web, a política da API): desembrulha a chave
    de uma execução para servir o artefato e nada mais (nem segredo, nem embrulho)."""
    bao = FakeBao()
    company, run = new_id(), new_id()
    bao.put_secret(f"empresas/{company}/portal/acesso", {"password": "senha-de-teste"})
    dispatcher = bao_client(bao, tmp_path, role=ROLE)
    web = bao_client(bao, tmp_path, role=WEB_ROLE)
    try:
        data_key = os.urandom(32)
        wrapped = await TransitCompanyKeys(dispatcher).wrap(company, run, data_key)
        assert await TransitCompanyKeys(web).unwrap(company, run, wrapped) == data_key
        with pytest.raises(VaultDenied):
            await web.read_kv("rapa", f"empresas/{company}/portal/acesso")
        with pytest.raises(KeyUnavailable):
            await TransitCompanyKeys(web).wrap(company, new_id(), os.urandom(32))
    finally:
        await dispatcher.aclose()
        await web.aclose()
    assert {policies for _, _, policies in bao.refusals} == {(API_ROLE,)}


def test_the_script_is_strict_bash_without_secrets() -> None:
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)  # noqa: S603, S607
    text = SCRIPT.read_text()
    assert "set -euo pipefail" in text
    assert os.access(SCRIPT, os.X_OK)
    for leak in ("hvs.", "BAO_TOKEN=", "password", "secret_id", "root"):
        assert leak not in text, leak


#: Um CLI "bao" de mentira: guarda os pedidos e lembra o que foi montado.
_FAKE_CLI = r"""#!/usr/bin/env bash
set -euo pipefail
state="$FAKE_BAO_STATE"
echo "$*" >> "$state/calls"
mount=""
for arg in "$@"; do
  case "$arg" in -path=*) mount="${arg#-path=}" ;; esac
done
listing() {
  printf '{'
  if [ -f "$state/$1" ]; then while read -r m; do printf '"%s/":{},' "$m"; done < "$state/$1"; fi
  printf '"sys/":{}}\n'
}
case "$1 ${2:-}" in
  "secrets list") listing secrets ;;
  "auth list") listing auth ;;
  "secrets enable") echo "$mount" >> "$state/secrets" ;;
  "auth enable") echo "$mount" >> "$state/auth" ;;
  "policy write") cat > "$state/policy-$3" ;;
  *) ;;
esac
"""


def _run_script(tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    cli = bindir / "bao"
    cli.write_text(_FAKE_CLI)
    cli.chmod(0o755)
    state = tmp_path / "estado"
    state.mkdir(exist_ok=True)
    return subprocess.run(  # noqa: S603
        [str(SCRIPT)],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{bindir}:/usr/bin:/bin",
            "BAO_ADDR": "https://openbao.cofre.test:8200",
            "FAKE_BAO_STATE": str(state),
            **env,
        },
        check=False,
    )


def test_the_script_is_idempotent_and_follows_the_configured_mounts(tmp_path: Path) -> None:
    state = tmp_path / "estado"
    for _ in range(2):
        done = _run_script(tmp_path, RAPA_BAO_KV="cofre-rapa", RAPA_NAMESPACE="plataforma")
        assert done.returncode == 0, done.stderr
    calls = (state / "calls").read_text().splitlines()
    # Montou cada coisa uma vez só, mesmo rodando duas vezes.
    enables = [call for call in calls if " enable " in f" {call} "]
    assert enables == [
        "secrets enable -path=cofre-rapa -version=2 kv",
        "secrets enable -path=rapa-transit transit",
        "auth enable -path=kubernetes kubernetes",
    ]
    policy = (state / "policy-rapa-despachante").read_text()
    assert 'path "cofre-rapa/data/empresas/*"' in policy and 'path "rapa/' not in policy
    roles = [call for call in calls if call.startswith("write auth/kubernetes/role/")]
    assert len(roles) == 8  # quatro papéis, duas vezes (reescritos iguais)
    # Cada papel só aceita a própria conta de serviço; a interface e a API só desembrulham.
    for role, account, policies in (
        ("rapa-despachante", "rapa-despachante", "rapa-despachante"),
        ("rapa-web", "rapa-web", "rapa-api"),
        ("rapa-api", "rapa-api", "rapa-api"),
        ("rapa-operacao", "rapa-operacao", "rapa-operacao"),
    ):
        call = next(call for call in roles if f"role/{role} " in call)
        for part in (
            f"bound_service_account_names={account} ",
            "bound_service_account_namespaces=plataforma",
            "audience=openbao",
            f"token_policies={policies} ",
            "token_no_default_policy=true",
        ):
            assert part in f"{call} ", (role, part)
    # Com o OpenBao no cluster, ele usa o CA e o token do próprio pod.
    assert "write auth/kubernetes/config kubernetes_host=https://kubernetes.default.svc" in calls
    # Fora do cluster, o CA da API vem de KUBERNETES_CA_CERT e o token da TokenReview de
    # KUBERNETES_TOKEN_REVIEWER_JWT: sem ele, o OpenBao mandaria à API o token de quem entra
    # (audiência openbao, que a API recusa). Sem o arquivo, o script para antes de mexer.
    ca = tmp_path / "ca.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\n")
    reviewer = tmp_path / "revisor.jwt"
    reviewer.write_text("jwt-de-teste")
    empty = tmp_path / "vazio.jwt"
    empty.write_text("")
    before = len((state / "calls").read_text().splitlines())
    for missing_reviewer in (
        {},
        {"KUBERNETES_TOKEN_REVIEWER_JWT": str(tmp_path / "nao.jwt")},
        {"KUBERNETES_TOKEN_REVIEWER_JWT": str(empty)},
    ):
        refused = _run_script(tmp_path, KUBERNETES_CA_CERT=str(ca), **missing_reviewer)
        assert refused.returncode == 1 and "KUBERNETES_TOKEN_REVIEWER_JWT" in refused.stderr
    assert len((state / "calls").read_text().splitlines()) == before
    done = _run_script(
        tmp_path, KUBERNETES_CA_CERT=str(ca), KUBERNETES_TOKEN_REVIEWER_JWT=str(reviewer)
    )
    assert done.returncode == 0, done.stderr
    last = (state / "calls").read_text().splitlines()
    assert any(
        call.startswith("write auth/kubernetes/config ")
        and f"kubernetes_ca_cert=@{ca}" in call
        and f"token_reviewer_jwt=@{reviewer}" in call
        and "disable_local_ca_jwt=true" in call
        for call in last
    )
    missing = _run_script(
        tmp_path,
        KUBERNETES_CA_CERT=str(tmp_path / "nao-existe.pem"),
        KUBERNETES_TOKEN_REVIEWER_JWT=str(reviewer),
    )
    assert missing.returncode == 1 and "KUBERNETES_CA_CERT" in missing.stderr
    # Um nome fora da regra não chega ao OpenBao.
    refused = _run_script(tmp_path, RAPA_BAO_KV="../sys")
    assert refused.returncode == 1 and "montagem inválida" in refused.stderr
    assert _run_script(tmp_path, BAO_ADDR="").returncode != 0


def _run_custodian(tmp_path: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    cli = bindir / "bao"
    cli.write_text(_FAKE_CLI)
    cli.chmod(0o755)
    state = tmp_path / "estado"
    state.mkdir(exist_ok=True)
    return subprocess.run(  # noqa: S603
        [str(CUSTODIAN), *args],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{bindir}:/usr/bin:/bin",
            "BAO_ADDR": "https://openbao.cofre.test:8200",
            "FAKE_BAO_STATE": str(state),
            **env,
        },
        check=False,
    )


def test_the_custodian_of_a_company_only_writes_its_secrets(tmp_path: Path) -> None:
    """Quem grava os segredos é o custodiante de cada empresa, direto no OpenBao (o token
    raiz é revogado depois do preparo): uma política por empresa, só debaixo do prefixo
    dela, que grava e lista, nunca lê o valor nem alcança outra empresa."""
    subprocess.run(["bash", "-n", str(CUSTODIAN)], check=True)  # noqa: S603, S607
    assert os.access(CUSTODIAN, os.X_OK)
    assert "set -euo pipefail" in CUSTODIAN.read_text()
    company, other = new_id(), new_id()
    done = _run_custodian(tmp_path, str(company), RAPA_BAO_KV="cofre-rapa")
    assert done.returncode == 0, done.stderr
    text = (tmp_path / "estado" / f"policy-rapa-custodiante-{company}").read_text()
    policy = parse_policy(text)
    assert {path: rule.capabilities for path, rule in policy.items()} == {
        f"cofre-rapa/data/empresas/{company}/*": {"create", "update"},
        f"cofre-rapa/metadata/empresas/{company}/*": {"list"},
        "sys/internal/ui/mounts/cofre-rapa": {"read"},
    }
    acl = ACL([policy])
    mine = f"cofre-rapa/data/empresas/{company}/portal/acesso"
    assert acl.allows(mine, "create") and acl.allows(mine, "update")
    assert not acl.allows(mine, "read") and not acl.allows(mine, "delete")
    assert acl.allows(f"cofre-rapa/metadata/empresas/{company}/", "list")
    for path in (
        f"cofre-rapa/data/empresas/{other}/portal/acesso",
        f"cofre-rapa/data/empresas/{company}",
        "cofre-rapa/data/empresas/x",
        f"rapa/data/empresas/{company}/portal/acesso",
    ):
        assert not acl.allows(path, "create") and not acl.allows(path, "update"), path
    assert not acl.allows("cofre-rapa/metadata/empresas/", "list")
    # Só o id de uma empresa (o UUID, em minúsculas): nada que mude o caminho.
    for bad in ("*", "../sys", f"{company}/*", str(company).upper(), ""):
        refused = _run_custodian(tmp_path, bad)
        assert refused.returncode != 0, bad
    assert _run_custodian(tmp_path).returncode != 0
    # A ordem de instalação cria os custodiantes antes de revogar o token raiz.
    operation = (ROOT / "docs" / "OPERACAO.md").read_text(encoding="utf-8")
    vault = operation[operation.index("### 1.6 Cofre") : operation.index("### 1.7")]
    assert "deploy/openbao/custodiante.sh" in vault
    assert vault.index("custodiante.sh") < vault.index("bao token revoke")


# -- bao server -dev (só com o binário no PATH) ---------------------------------------

#: Com ``1`` (no CI, que baixa o binário oficial do OpenBao, conferido pelo SHA-256), o teste
#: contra o ``bao server -dev`` falha em vez de ser pulado quando não há ``bao``.
BAO_REQUIRED_ENV = "RAPA_TEST_BAO_OBRIGATORIO"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def bao_dev(tmp_path: Path) -> Iterator[tuple[str, str]]:
    binary = shutil.which("bao")
    if binary is None:
        if os.environ.get(BAO_REQUIRED_ENV) == "1":
            pytest.fail(f"o binário 'bao' (OpenBao) não está no PATH, e {BAO_REQUIRED_ENV}=1")
        pytest.skip("o binário 'bao' (OpenBao) não está no PATH")
    token = "dev-" + secrets.token_hex(8)
    address = f"http://127.0.0.1:{_free_port()}"
    process = subprocess.Popen(  # noqa: S603
        [
            binary,
            "server",
            "-dev",
            f"-dev-root-token-id={token}",
            f"-dev-listen-address={address.removeprefix('http://')}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                if httpx2.get(f"{address}/v1/sys/health", timeout=1).status_code == 200:
                    break
            except httpx2.HTTPError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError("o bao server -dev não subiu")
            time.sleep(0.2)
        yield address, token
    finally:
        process.terminate()
        process.wait(timeout=10)


def _role_token(address: str, root: str, policy: str) -> str:
    """Um token só com a política do papel (sem a "default"), como a entrada do papel."""
    response = httpx2.post(
        f"{address}/v1/auth/token/create",
        headers={"X-Vault-Token": root},
        json={"policies": [policy], "no_default_policy": True, "ttl": "15m"},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["auth"]["client_token"])


async def test_everything_against_bao_server_dev(bao_dev: tuple[str, str], tmp_path: Path) -> None:
    """O preparar.sh de verdade, e cada papel com um token só da política dele."""
    address, token = bao_dev
    # Fora de um pod, o OpenBao não tem o CA da API do Kubernetes montado: vem do script.
    ca, _, _ = make_tls(tmp_path)
    reviewer = tmp_path / "revisor.jwt"
    reviewer.write_text("jwt-de-teste")
    env = {
        **os.environ,
        "BAO_ADDR": address,
        "BAO_TOKEN": token,
        "KUBERNETES_HOST": "https://127.0.0.1:6443",
        "KUBERNETES_CA_CERT": str(ca),
        "KUBERNETES_TOKEN_REVIEWER_JWT": str(reviewer),
    }
    for _ in range(2):  # idempotente de verdade
        await asyncio.to_thread(
            subprocess.run, [str(SCRIPT)], env=env, check=True, capture_output=True
        )
    company, other = new_id(), new_id()
    headers = {"X-Vault-Token": token}
    # Fora do cluster, a TokenReview usa o token informado (sem ele, o OpenBao mandaria o
    # token de quem entra, com a audiência openbao, que a API recusa), e rodar de novo o
    # mantém.
    config = httpx2.get(f"{address}/v1/auth/kubernetes/config", headers=headers)
    assert config.status_code == 200, config.text
    assert config.json()["data"]["token_reviewer_jwt_set"] is True
    assert config.json()["data"]["disable_local_ca_jwt"] is True
    for who in (company, other):
        for path in ("portal/acesso", "fiscal/a1"):
            response = httpx2.post(
                f"{address}/v1/rapa/data/empresas/{who}/{path}",
                headers=headers,
                json={"data": {"password": "senha-de-teste"}},
            )
            assert response.status_code == 200, response.text
    policy = httpx2.get(f"{address}/v1/sys/policy/rapa-despachante", headers=headers)
    assert "rapa/data/empresas/*" in json.dumps(policy.json())

    async def key_state(name: str) -> dict[str, Any] | None:
        response = httpx2.get(f"{address}/v1/rapa-transit/keys/{name}", headers=headers)
        if response.status_code == 404:
            return None
        assert response.status_code == 200, response.text
        return dict(response.json()["data"])

    clients = []
    for role in (ROLE, API_ROLE, OPERATION_ROLE):
        token_file = tmp_path / f"token-{role}"
        token_file.write_text(_role_token(address, token, role))
        clients.append(OpenBaoClient(BaoSettings(address=address, token_file=token_file, dev=True)))
    try:
        assert await BaoReadiness(clients[0]).ready() is True
        dispatcher, api, operation = clients
        await _each_role_does_its_work_and_nothing_more(
            dispatcher, api, operation, key_state, company, other
        )
    finally:
        for client in clients:
            await client.aclose()


async def test_the_custodian_policy_against_bao_server_dev(
    bao_dev: tuple[str, str], tmp_path: Path
) -> None:
    """A política do custodiante de verdade: grava na própria empresa, nunca noutra."""
    address, token = bao_dev
    env = {**os.environ, "BAO_ADDR": address, "BAO_TOKEN": token}
    reviewer = tmp_path / "revisor.jwt"
    reviewer.write_text("jwt-de-teste")
    await asyncio.to_thread(
        subprocess.run,
        [str(SCRIPT)],
        env={
            **env,
            "KUBERNETES_HOST": "https://127.0.0.1:6443",
            "KUBERNETES_CA_CERT": str(make_tls(tmp_path)[0]),
            "KUBERNETES_TOKEN_REVIEWER_JWT": str(reviewer),
        },
        check=True,
        capture_output=True,
    )
    company, other = new_id(), new_id()
    await asyncio.to_thread(
        subprocess.run, [str(CUSTODIAN), str(company)], env=env, check=True, capture_output=True
    )
    custodian = _role_token(address, token, f"rapa-custodiante-{company}")
    headers = {"X-Vault-Token": custodian}
    body = {"data": {"password": "senha-de-teste"}}
    mine = httpx2.post(
        f"{address}/v1/rapa/data/empresas/{company}/portal/acesso", headers=headers, json=body
    )
    assert mine.status_code == 200, mine.text
    again = httpx2.post(
        f"{address}/v1/rapa/data/empresas/{company}/portal/acesso", headers=headers, json=body
    )
    assert again.status_code == 200, again.text
    for url in (
        f"{address}/v1/rapa/data/empresas/{other}/portal/acesso",
        f"{address}/v1/rapa/data/empresas/{company}",
    ):
        assert httpx2.post(url, headers=headers, json=body).status_code == 403, url
    read = httpx2.get(f"{address}/v1/rapa/data/empresas/{company}/portal/acesso", headers=headers)
    assert read.status_code == 403
