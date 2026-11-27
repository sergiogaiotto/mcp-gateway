"""O teste ponta a ponta do job kind do CI, de fora do cluster (pelo kubectl port-forward).

Uso (depois de deploy/kind/instalar.sh)::

    python deploy/kind/ponta_a_ponta.py --api http://127.0.0.1:18000 \\
        --bao http://127.0.0.1:18200 --filer http://127.0.0.1:18888 \\
        --portal http://portal.rapa.test:8080 --token-admin TOKEN --token-criadora TOKEN

1. Quem administra a empresa grava o login e o TOTP do portal de teste no KV do OpenBao, em
   ``<kv>/data/empresas/<empresa>/portal/...`` (a parte da empresa é sempre posta pelo
   RaPA), e registra as duas credenciais pela REST (só a referência ao cofre).
2. A criadora cria e valida o robô de exemplo que entra no portal e baixa as notas (a
   validação roda num pod de uso único); quem administra publica.
3. Uma execução pela REST, até ``succeeded``.
4. Os artefatos: lidos de volta pela API (o SHA-256 confere) e, no S3 (o filer do
   SeaweedFS), só cifrados: todo objeto da execução termina em ``.enc`` e nenhum tem o
   conteúdo em claro.

Os tokens são os de desenvolvimento do kind (values-kind.yaml); nenhum é segredo de alguém.
"""

import argparse
import asyncio
import hashlib
import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx2

from rapa.fixtures.portal import DEFAULT_PASSWORD, DEFAULT_TOTP_SECRET, DEFAULT_USER
from rapa.fixtures.robots import baixar_notas

__all__ = [
    "Artifact",
    "check_encrypted",
    "main",
    "publish_and_run",
    "register_credentials",
    "write_portal_secrets",
]

#: Onde ficam as credenciais do portal de teste no cofre, abaixo da pasta da empresa.
VAULT_PATHS = {"portal": "portal/login", "portal_otp": "portal/otp"}


@dataclass(frozen=True)
class Artifact:
    """Um artefato da execução, como a API o entrega."""

    name: str
    sha256: str
    content: bytes


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _ok(response: httpx2.Response, *expected: int) -> Any:
    if response.status_code not in (expected or (200,)):
        raise AssertionError(
            f"{response.request.method} {response.request.url.path}: "
            f"{response.status_code} {response.text[:500]}"
        )
    return response.json() if response.content else None


async def company_and_area(api: httpx2.AsyncClient, admin: str) -> tuple[str, str]:
    me = _ok(await api.get("/v1/eu", headers=_auth(admin)))
    areas = _ok(await api.get("/v1/empresa/areas", headers=_auth(admin)))
    if not areas:
        raise AssertionError("a empresa de teste não tem área")
    return str(me["company_id"]), str(areas[0]["area_id"])


async def write_portal_secrets(
    bao: httpx2.AsyncClient, root_token: str, kv: str, company: str
) -> None:
    """Grava o login e o TOTP do portal de teste no KV v2, na pasta da empresa."""
    values = {
        "portal": {"username": DEFAULT_USER, "password": DEFAULT_PASSWORD},
        "portal_otp": {"totp_secret": DEFAULT_TOTP_SECRET},
    }
    for name, path in VAULT_PATHS.items():
        response = await bao.post(
            f"/v1/{kv}/data/empresas/{company}/{path}",
            json={"data": values[name]},
            headers={"X-Vault-Token": root_token},
        )
        _ok(response, 200, 204)


async def register_credentials(api: httpx2.AsyncClient, admin: str, area: str, portal: str) -> None:
    """Registra as duas credenciais do robô (só a referência ao cofre) pela REST."""
    for name, kind in (("portal", "password"), ("portal_otp", "totp")):
        body = {
            "area_id": area,
            "nome": name,
            "tipo": kind,
            "origem": portal,
            "caminho_cofre": VAULT_PATHS[name],
        }
        _ok(await api.post("/v1/credenciais", json=body, headers=_auth(admin)), 201)


async def publish_and_run(
    api: httpx2.AsyncClient,
    *,
    area: str,
    portal: str,
    creator: str,
    approver: str,
    operator: str,
    timeout_s: float,
) -> tuple[dict[str, Any], list[Artifact]]:
    """Cria, valida e publica o robô de exemplo; executa pela REST e lê os artefatos."""
    host = urlsplit(portal).hostname or ""
    manifest = baixar_notas(portal, host).model_dump(mode="json")
    created = await api.post(
        "/v1/robos", json={"area_id": area, "manifesto": manifest}, headers=_auth(creator)
    )
    _ok(created, 201)
    version = created.headers["location"]
    _ok(await api.post(f"{version}/validar", headers=_auth(creator)))
    published = _ok(await api.post(f"{version}/publicar", headers=_auth(approver)))
    if published["status"] != "published":
        raise AssertionError(f"o robô não foi publicado: {published['status']}")
    started = await api.post(
        f"/v1/catalogo/{manifest['name']}/execucoes",
        json={"entradas": {}},
        headers=_auth(operator),
    )
    run = _ok(started, 202)
    status = await wait_finished(api, operator, str(run["run_id"]), timeout_s)
    if status["state"] != "succeeded":
        raise AssertionError(
            f"a execução terminou {status['state']}: {status.get('error_code')} "
            f"{status.get('error_message')}"
        )
    listed = _ok(await api.get(f"/v1/execucoes/{run['run_id']}/artefatos", headers=_auth(operator)))
    artifacts = []
    for item in listed:
        response = await api.get(
            f"/v1/execucoes/{run['run_id']}/artefatos/{item['name']}", headers=_auth(operator)
        )
        if response.status_code != 200:
            raise AssertionError(f"o artefato {item['name']} não abriu: {response.status_code}")
        content = response.content
        if hashlib.sha256(content).hexdigest() != item["sha256"]:
            raise AssertionError(f"o artefato {item['name']} não confere com o SHA-256")
        artifacts.append(Artifact(name=item["name"], sha256=item["sha256"], content=content))
    if not artifacts:
        raise AssertionError("a execução deu certo, mas sem artefatos")
    return status, artifacts


async def wait_finished(
    api: httpx2.AsyncClient, token: str, run_id: str, timeout_s: float
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        status: dict[str, Any] = _ok(await api.get(f"/v1/execucoes/{run_id}", headers=_auth(token)))
        if status.get("finished_at"):
            return status
        await asyncio.sleep(1.0)
    raise AssertionError(f"a execução {run_id} não terminou em {timeout_s:.0f} s")


async def _filer_files(filer: httpx2.AsyncClient, path: str) -> list[str]:
    """Os arquivos abaixo de ``path`` no filer do SeaweedFS (a listagem em JSON)."""
    response = await filer.get(path.rstrip("/") + "/", headers={"Accept": "application/json"})
    if response.status_code == 404:
        return []
    listing = _ok(response) or {}
    files: list[str] = []
    for entry in listing.get("Entries") or []:
        full = str(entry["FullPath"])
        # Mode é o os.FileMode do Go: o bit 31 marca as pastas.
        if int(entry.get("Mode", 0)) & _GO_MODE_DIR:
            files += await _filer_files(filer, full)
        else:
            files.append(full)
    return files


_GO_MODE_DIR = 1 << 31


async def check_encrypted(
    filer: httpx2.AsyncClient,
    *,
    bucket: str,
    company: str,
    run_id: str,
    artifacts: list[Artifact],
) -> list[str]:
    """Confere que os artefatos da execução estão no bucket só cifrados (``.enc``)."""
    prefix = f"/buckets/{bucket}/empresas/{company}/execucoes/{run_id}"
    objects = await _filer_files(filer, prefix)
    if not objects:
        raise AssertionError(f"nenhum objeto em {prefix}")
    for path in objects:
        if not path.endswith(".enc"):
            raise AssertionError(f"objeto sem cifra no bucket: {path}")
        raw = (await filer.get(path)).content
        for artifact in artifacts:
            sample = artifact.content[:64]
            if len(sample) >= 16 and sample in raw:
                raise AssertionError(f"{path} tem o conteúdo de {artifact.name} em claro")
    names = {path.rsplit("/", 1)[-1].removesuffix(".enc") for path in objects}
    missing = {a.name for a in artifacts} - names
    if missing:
        raise AssertionError(f"artefatos que não estão no bucket: {sorted(missing)}")
    return objects


async def run(args: argparse.Namespace) -> None:
    async with (
        httpx2.AsyncClient(base_url=args.api, timeout=60.0, trust_env=False) as api,
        httpx2.AsyncClient(base_url=args.bao, timeout=30.0, trust_env=False) as bao,
        httpx2.AsyncClient(base_url=args.filer, timeout=30.0, trust_env=False) as filer,
    ):
        company, area = await company_and_area(api, args.token_admin)
        await write_portal_secrets(bao, args.bao_token, args.kv, company)
        await register_credentials(api, args.token_admin, area, args.portal)
        status, artifacts = await publish_and_run(
            api,
            area=area,
            portal=args.portal,
            creator=args.token_criadora,
            approver=args.token_admin,
            operator=args.token_admin,
            timeout_s=args.prazo,
        )
        objects = await check_encrypted(
            filer,
            bucket=args.bucket,
            company=company,
            run_id=str(status["run_id"]),
            artifacts=artifacts,
        )
        print(
            f"execução {status['run_id']}: {status['state']}; {len(artifacts)} artefatos lidos "
            f"pela API e {len(objects)} objetos cifrados no S3"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ponta_a_ponta", description=__doc__.split("\n")[0])
    parser.add_argument("--api", required=True)
    parser.add_argument("--bao", required=True)
    parser.add_argument("--bao-token", default="root", help="o token raiz do OpenBao de dev")
    parser.add_argument("--kv", default="rapa")
    parser.add_argument("--filer", required=True)
    parser.add_argument("--bucket", default="rapa-artefatos")
    parser.add_argument("--portal", required=True)
    parser.add_argument("--token-admin", required=True)
    parser.add_argument("--token-criadora", required=True)
    parser.add_argument("--prazo", type=float, default=600.0)
    args = parser.parse_args(argv)
    try:
        asyncio.run(run(args))
    except (AssertionError, httpx2.HTTPError) as exc:
        print(f"ponta_a_ponta: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
