"""A escolha do cofre, do armazenamento e das chaves pela configuração, conferida na partida."""

import json
from pathlib import Path
from typing import Any

import pytest

from rapa.control.artifacts import LocalArtifactStore, S3ArtifactStore
from rapa.control.backends import (
    Backends,
    BackendSettings,
    KeysKind,
    StorageKind,
    VaultKind,
)
from rapa.control.db import Database
from rapa.control.keys import LocalCompanyKeys, TransitCompanyKeys
from rapa.control.launcher import LocalProcessLauncher, NoSecrets, StaticSecretSource
from rapa.control.services.lifecycle import LocalKeyDestroyer
from rapa.control.vault import OpenBaoKeyDestroyer, OpenBaoSecretSource
from rapa.core.ids import new_id
from rapa.server import ServerSettings, create_server_app

from .fakebao import FakeBao
from .support import bao_client
from .tls import make_tls, serve

AWS_SECRET = "segredo-do-s3-que-nao-pode-vazar"
MASTER = "chave-mestra-com-mais-de-trinta-e-dois-caracteres"
S3 = {
    "RAPA_ARMAZENAMENTO": "s3",
    "RAPA_S3_ENDERECO": "https://seaweedfs.armazenamento.test:8333",
    "RAPA_S3_BUCKET": "rapa-artefatos",
    "AWS_ACCESS_KEY_ID": "chave-do-s3",
    "AWS_SECRET_ACCESS_KEY": AWS_SECRET,
}
BAO = {
    "RAPA_COFRE": "openbao",
    "RAPA_BAO_ENDERECO": "https://openbao.cofre.test:8200",
    "RAPA_BAO_PAPEL": "rapa-despachante",
}
DB = Database.from_url("postgresql://rapa:senha@banco.test:5432/rapa")


def test_defaults_are_local_storage_and_no_vault(tmp_path: Path) -> None:
    settings = BackendSettings.from_env({})
    assert (settings.vault, settings.storage, settings.keys_kind) == (
        VaultKind.NONE,
        StorageKind.LOCAL,
        KeysKind.LOCAL,
    )
    backends = Backends(settings)
    assert isinstance(backends.artifact_store(tmp_path, DB), LocalArtifactStore)
    assert isinstance(backends.secret_source(DB, None), NoSecrets)
    assert isinstance(backends.key_destroyer(), LocalKeyDestroyer)
    assert backends.readiness_checks() == ()


def test_dev_secrets_file_is_the_dev_vault(tmp_path: Path) -> None:
    secrets = tmp_path / "segredos.json"
    secrets.write_text(json.dumps({str(new_id()): {}}))
    env = {"RAPA_DEV": "1", "RAPA_SEGREDOS_DEV": str(secrets)}
    settings = BackendSettings.from_env(env)
    assert settings.vault is VaultKind.DEV
    assert isinstance(Backends(settings).secret_source(DB, secrets), StaticSecretSource)


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"RAPA_COFRE": "dev"}, "RAPA_COFRE=dev só vale com RAPA_DEV=1"),
        ({"RAPA_COFRE": "talvez"}, "RAPA_COFRE precisa ser um de"),
        ({"RAPA_COFRE": "openbao"}, "RAPA_BAO_ENDERECO e RAPA_BAO_PAPEL"),
        ({**BAO, "RAPA_BAO_PAPEL": ""}, "RAPA_BAO_PAPEL"),
        ({**BAO, "RAPA_BAO_ENDERECO": "http://openbao.cofre.test:8200"}, "https"),
        ({**BAO, "RAPA_BAO_TOKEN_ARQUIVO": "/tmp/token"}, "RAPA_DEV=1"),
        ({"RAPA_CHAVES": "openbao"}, "RAPA_BAO_ENDERECO"),
        ({"RAPA_ARMAZENAMENTO": "s3"}, "RAPA_S3_ENDERECO, RAPA_S3_BUCKET"),
        ({**S3, "RAPA_S3_BUCKET": ""}, "RAPA_S3_BUCKET"),
        ({**S3, "AWS_SECRET_ACCESS_KEY": ""}, "AWS_SECRET_ACCESS_KEY"),
        ({**S3, "RAPA_S3_ENDERECO": "http://seaweedfs.test:8333"}, "https"),
        # As chaves locais com o S3: só em desenvolvimento ou com a chave mestra.
        (S3, "RAPA_DEV=1 ou com a chave mestra"),
        ({**S3, "RAPA_CHAVE_ARTEFATOS": "curta-demais"}, "artifact_key"),
    ],
)
def test_startup_checks_fail_closed_without_values(env: dict[str, str], message: str) -> None:
    with pytest.raises(ValueError, match=message) as caught:
        BackendSettings.from_env(env)
    text = str(caught.value)
    for value in (AWS_SECRET, "curta-demais"):
        assert value not in text


def test_s3_with_the_master_key_or_in_dev(tmp_path: Path) -> None:
    settings = BackendSettings.from_env({**S3, "RAPA_CHAVE_ARTEFATOS": MASTER})
    assert settings.s3 is not None and settings.s3.path_style and settings.s3.region == "us-east-1"
    assert AWS_SECRET not in repr(settings) and MASTER not in repr(settings)
    backends = Backends(settings)
    store = backends.artifact_store(tmp_path, DB)
    assert isinstance(store, S3ArtifactStore) and store.bucket == "rapa-artefatos"
    assert isinstance(store.keys, LocalCompanyKeys)
    assert [check.name for check in backends.readiness_checks()] == ["s3"]
    # O cliente usa só as credenciais dadas, no endereço dado, com o estilo de caminho.
    client = store.client
    credentials = client._request_signer._credentials
    assert (credentials.access_key, credentials.secret_key) == ("chave-do-s3", AWS_SECRET)
    assert client.meta.endpoint_url == S3["RAPA_S3_ENDERECO"]
    assert client.meta.config.s3["addressing_style"] == "path"
    virtual = BackendSettings.from_env(
        {**S3, "RAPA_CHAVE_ARTEFATOS": MASTER, "RAPA_S3_ESTILO_CAMINHO": "0"}
    )
    assert virtual.s3 is not None and not virtual.s3.path_style
    # Em desenvolvimento sem a chave mestra: uma chave sorteada, a mesma no processo todo
    # (a API e o despachante abrem os mesmos artefatos).
    dev = BackendSettings.from_env({**S3, "RAPA_DEV": "1"})
    first = Backends(dev).artifact_store(tmp_path, DB)
    second = Backends(dev).artifact_store(tmp_path, DB)
    assert isinstance(first, S3ArtifactStore) and isinstance(second, S3ArtifactStore)
    assert isinstance(first.keys, LocalCompanyKeys) and isinstance(second.keys, LocalCompanyKeys)
    assert first.keys._master == second.keys._master


async def test_openbao_everywhere(tmp_path: Path) -> None:
    settings = BackendSettings.from_env({**BAO, **S3})
    assert settings.keys_kind is KeysKind.OPENBAO
    bao = FakeBao()
    client = bao_client(bao, tmp_path)
    backends = Backends(settings, bao=client)
    store = backends.artifact_store(tmp_path, DB)
    assert isinstance(store, S3ArtifactStore) and isinstance(store.keys, TransitCompanyKeys)
    assert isinstance(backends.secret_source(DB, None), OpenBaoSecretSource)
    destroyer = backends.key_destroyer(store)
    assert isinstance(destroyer, OpenBaoKeyDestroyer)
    assert (destroyer.secrets, destroyer.transit, destroyer.store) == (True, True, store)
    checks = backends.readiness_checks()
    assert [check.name for check in checks] == ["openbao", "s3"]
    assert await checks[0].ready() is True
    bao.sealed = True
    assert await checks[0].ready() is False
    await backends.aclose()
    # Só as chaves no OpenBao (os segredos de outro jeito): o destruidor só cuida do transit.
    keys_only = BackendSettings.from_env(
        {**S3, "RAPA_CHAVES": "openbao", **{k: v for k, v in BAO.items() if k != "RAPA_COFRE"}}
    )
    only = Backends(keys_only, bao=bao_client(bao, tmp_path)).key_destroyer()
    assert isinstance(only, OpenBaoKeyDestroyer) and (only.secrets, only.transit) == (False, True)


def _server_env(**extra: str) -> dict[str, str]:
    return {
        "RAPA_DATABASE_URL": "postgresql://rapa_app:senha@banco.test:5432/rapa",
        "RAPA_DISPATCHER_DATABASE_URL": "postgresql://rapa_dispatcher:senha@banco.test:5432/rapa",
        "RAPA_JWT_ISSUER": "https://login.test/realms/rapa",
        "RAPA_JWT_AUDIENCE": "rapa-api",
        "RAPA_JWKS_URL": "https://login.test/realms/rapa/protocol/openid-connect/certs",
        "RAPA_UI": "0",
        **extra,
    }


def test_the_server_wires_the_chosen_vault_store_and_checks() -> None:
    settings = ServerSettings.from_env(_server_env(**BAO, **S3))
    assert settings.backends.vault is VaultKind.OPENBAO
    app = create_server_app(settings)
    dispatcher = app.state.dispatcher
    launcher: LocalProcessLauncher = dispatcher.launch
    assert isinstance(launcher.secrets, OpenBaoSecretSource)
    assert isinstance(dispatcher.artifacts, S3ArtifactStore)
    assert isinstance(app.state.artifacts.store, S3ArtifactStore)
    # A API e o despachante usam a mesma chave da empresa (o mesmo cliente do OpenBao).
    assert dispatcher.artifacts.keys is app.state.artifacts.store.keys
    assert [check.name for check in app.state.readiness_checks] == ["openbao", "s3"]
    # Sem nada configurado: o de sempre.
    plain = create_server_app(ServerSettings.from_env(_server_env()))
    assert isinstance(plain.state.dispatcher.launch.secrets, NoSecrets)
    assert isinstance(plain.state.dispatcher.artifacts, LocalArtifactStore)
    assert plain.state.readiness_checks == ()


async def test_the_vault_and_s3_checks_are_part_of_saude_pronto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Com o OpenBao e o S3 configurados, ``/saude/pronto`` também os confere: o cofre
    selado ou o bucket fora deixam o processo não pronto, pelo nome e nada mais."""
    from rapa.control.artifacts import S3Readiness
    from rapa.control.health import DatabaseCheck, DispatcherCheck
    from rapa.control.vault import BaoReadiness

    app = create_server_app(ServerSettings.from_env(_server_env(**BAO, **S3)))
    readiness = app.state.readiness
    assert [check.name for check in readiness.checks] == [
        "banco",
        "banco_despachante",
        "despachante",
        "openbao",
        "s3",
    ]
    state = {"openbao": False, "s3": False}

    async def database_ready(self: object) -> bool:
        return True

    async def bao_ready(self: BaoReadiness) -> bool:
        return state["openbao"]

    async def s3_ready(self: S3Readiness) -> bool:
        return state["s3"]

    monkeypatch.setattr(DatabaseCheck, "ready", database_ready)
    monkeypatch.setattr(DispatcherCheck, "ready", database_ready)  # não subiu aqui
    monkeypatch.setattr(BaoReadiness, "ready", bao_ready)
    monkeypatch.setattr(S3Readiness, "ready", s3_ready)
    readiness.cache_s = 0.0
    response = await readiness.endpoint()
    assert response.status_code == 503
    assert json.loads(bytes(response.body)) == {
        "estado": "nao_pronto",
        "falhas": ["openbao", "s3"],
    }
    state.update(openbao=True, s3=True)
    assert (await readiness.endpoint()).status_code == 200
    # Sem o cofre e o S3, só o banco e o despachante.
    plain = create_server_app(ServerSettings.from_env(_server_env()))
    assert [check.name for check in plain.state.readiness.checks] == [
        "banco",
        "banco_despachante",
        "despachante",
    ]


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({**BAO, "RAPA_DEV": "1", "RAPA_SEGREDOS_DEV": "/tmp/s.json"}, "RAPA_COFRE=dev"),
        ({"RAPA_COFRE": "dev", "RAPA_DEV": "1"}, "RAPA_SEGREDOS_DEV"),
    ],
)
def test_the_server_refuses_mixed_vaults(extra: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        ServerSettings.from_env(_server_env(**extra))


class _S3Seen:
    """Um "S3" de teste que só responde ao HEAD do bucket e guarda o que recebeu."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, str]] = []

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        assert scope["type"] == "http"
        headers = {key.decode(): value.decode() for key, value in scope["headers"]}
        self.requests.append((scope["method"], scope["path"], headers.get("authorization", "")))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})


_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


async def test_the_s3_client_checks_tls_with_the_ca_and_signs_for_the_region(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RAPA_S3_CA confere o certificado do SeaweedFS; RAPA_S3_REGIAO vai na assinatura."""
    for name in _PROXY_VARS:
        monkeypatch.delenv(name, raising=False)
    ca, cert, key = make_tls(tmp_path)
    seen = _S3Seen()
    with serve(seen, cert, key) as port:
        env = {
            **S3,
            "RAPA_S3_ENDERECO": f"https://127.0.0.1:{port}",
            "RAPA_S3_REGIAO": "sa-east-1",
            "RAPA_CHAVE_ARTEFATOS": MASTER,
        }
        trusted = Backends(BackendSettings.from_env({**env, "RAPA_S3_CA": str(ca)}))
        untrusted = Backends(BackendSettings.from_env(env))  # só os CAs do sistema
        try:
            (check,) = untrusted.readiness_checks()
            assert await check.ready() is False
            assert seen.requests == []  # o certificado não valeu: nada foi enviado
            (check,) = trusted.readiness_checks()
            assert await check.ready() is True
        finally:
            await trusted.aclose()
            await untrusted.aclose()
    assert [(method, path) for method, path, _ in seen.requests] == [("HEAD", "/rapa-artefatos")]
    assert "/sa-east-1/s3/aws4_request" in seen.requests[0][2]
    assert trusted.s3_client.meta.region_name == "sa-east-1"


def test_the_s3_client_has_short_timeouts_and_few_retries() -> None:
    client = Backends(BackendSettings.from_env({**S3, "RAPA_CHAVE_ARTEFATOS": MASTER})).s3_client
    config = client.meta.config
    assert (config.connect_timeout, config.read_timeout) == (5, 30)
    # max_attempts=3 são três novas tentativas: quatro ao todo, no modo padrão.
    assert config.retries == {"total_max_attempts": 4, "mode": "standard"}
    assert config.signature_version == "s3v4"
    assert client.meta.region_name == "us-east-1"  # o padrão de RAPA_S3_REGIAO
