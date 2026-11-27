"""O cliente do OpenBao: entrada kubernetes, renovação, 403, TLS, redirecionamento e erros."""

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx2
import pytest
from pydantic import ValidationError

from rapa.control.vault import (
    BaoReadiness,
    BaoSettings,
    OpenBaoClient,
    VaultDenied,
    VaultError,
    VaultUnavailable,
)

from .fakebao import ROLE, SA_TOKEN, FakeBao, FakeClock
from .support import ADDRESS, bao_client, sa_token_file
from .tls import free_port, make_tls, serve

SECRET = "senha-de-teste-que-nao-pode-vazar"


def _bao(**changes: object) -> FakeBao:
    bao = FakeBao(**changes)  # type: ignore[arg-type]
    bao.put_secret("empresas/x/portal", {"username": "ana", "password": SECRET})
    return bao


async def test_login_uses_the_projected_token_and_reuses_the_bao_token(tmp_path: Path) -> None:
    bao = _bao()
    client = bao_client(bao, tmp_path)
    try:
        assert await client.read_kv("rapa", "empresas/x/portal") == {
            "username": "ana",
            "password": SECRET,
        }
        assert await client.read_kv("rapa", "empresas/x/outro") is None
    finally:
        await client.aclose()
    # Uma entrada só, com o papel e o token da conta de serviço lido do arquivo.
    assert bao.logins == 1
    login = next(item["body"] for item in bao.bodies if item["path"].startswith("auth/"))
    assert login == {"role": ROLE, "jwt": SA_TOKEN}


async def test_the_service_account_token_is_read_again_at_each_login(tmp_path: Path) -> None:
    """O kubelet troca o token projetado: cada entrada lê o arquivo de novo."""
    bao = _bao(jwt="token-novo")
    client = bao_client(bao, tmp_path)
    try:
        with pytest.raises(VaultDenied):
            await client.read_kv("rapa", "empresas/x/portal")
        sa_token_file(tmp_path, "token-novo")
        assert await client.read_kv("rapa", "empresas/x/portal") is not None
    finally:
        await client.aclose()


async def test_the_token_is_renewed_before_it_expires(tmp_path: Path) -> None:
    clock = FakeClock()
    bao = _bao(clock=clock, lease_s=60)
    client = bao_client(bao, tmp_path, clock=clock)
    try:
        await client.read_kv("rapa", "empresas/x/portal")
        clock.now += 30  # ainda longe do fim: nada a renovar
        await client.read_kv("rapa", "empresas/x/portal")
        assert (bao.logins, bao.renewals) == (1, 0)
        clock.now += 15  # passou de dois terços da validade: renova antes de vencer
        await client.read_kv("rapa", "empresas/x/portal")
        assert (bao.logins, bao.renewals) == (1, 1)
        clock.now += 200  # venceu sem renovar: entra de novo (renovar já não serve)
        await client.read_kv("rapa", "empresas/x/portal")
        assert (bao.logins, bao.renewals) == (2, 1)
    finally:
        await client.aclose()


async def test_a_token_that_cannot_be_renewed_is_replaced_before_it_expires(
    tmp_path: Path,
) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/login"):
            auth = {"client_token": "hvs.x", "lease_duration": 60, "renewable": False}
            return httpx2.Response(200, json={"auth": auth})
        return httpx2.Response(200, json={"data": {"data": {"password": "x"}}})

    clock = FakeClock()
    settings = BaoSettings(address=ADDRESS, role=ROLE, sa_token_file=sa_token_file(tmp_path))
    client = OpenBaoClient(settings, transport=httpx2.MockTransport(handler), clock=clock)
    try:
        await client.read_kv("rapa", "empresas/x/portal")
        clock.now += 45
        await client.read_kv("rapa", "empresas/x/portal")
    finally:
        await client.aclose()
    assert client.logins == 2


async def test_a_missing_ca_file_is_unavailable(tmp_path: Path) -> None:
    settings = BaoSettings(
        address=ADDRESS,
        role=ROLE,
        ca_file=tmp_path / "nao-existe.pem",
        sa_token_file=sa_token_file(tmp_path),
    )
    client = OpenBaoClient(settings)
    with pytest.raises(VaultUnavailable, match="RAPA_BAO_CA"):
        await client.read_kv("rapa", "empresas/x/portal")
    await client.aclose()


async def test_a_failed_renewal_logs_in_again(tmp_path: Path) -> None:
    clock = FakeClock()
    bao = _bao(clock=clock, lease_s=60)
    client = bao_client(bao, tmp_path, clock=clock)
    try:
        await client.read_kv("rapa", "empresas/x/portal")
        bao.renew_fails = True
        clock.now += 45
        assert await client.read_kv("rapa", "empresas/x/portal") is not None
    finally:
        await client.aclose()
    assert bao.logins == 2


async def test_a_403_logs_in_again_and_retries_once(tmp_path: Path) -> None:
    bao = _bao()
    client = bao_client(bao, tmp_path)
    try:
        await client.read_kv("rapa", "empresas/x/portal")
        bao.expire_all_tokens()  # o OpenBao esqueceu o token (reiniciou, foi revogado)
        assert await client.read_kv("rapa", "empresas/x/portal") is not None
        assert bao.logins == 2
        # A política nega de verdade: uma entrada nova, e a segunda 403 é uma recusa.
        bao.denied.add("empresas/x/")
        with pytest.raises(VaultDenied):
            await client.read_kv("rapa", "empresas/x/portal")
        assert bao.logins == 3
    finally:
        await client.aclose()


async def test_a_redirect_is_never_followed(tmp_path: Path) -> None:
    bao = _bao()
    bao.redirects.add("rapa/data/empresas/x/portal")
    client = bao_client(bao, tmp_path)
    try:
        with pytest.raises(VaultError, match="redirecionamento"):
            await client.read_kv("rapa", "empresas/x/portal")
    finally:
        await client.aclose()
    assert all("outro.test" not in path for _, path in bao.requests)


async def test_errors_never_carry_values_tokens_or_bodies(tmp_path: Path) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/login"):
            return httpx2.Response(
                200, json={"auth": {"client_token": "hvs.token-secreto", "lease_duration": 60}}
            )
        return httpx2.Response(500, json={"errors": [f"falhou lendo {SECRET}"]})

    settings = BaoSettings(address=ADDRESS, role=ROLE, sa_token_file=sa_token_file(tmp_path))
    client = OpenBaoClient(settings, transport=httpx2.MockTransport(handler))
    try:
        with pytest.raises(VaultError) as caught:
            await client.read_kv("rapa", "empresas/x/portal")
    finally:
        await client.aclose()
    message = str(caught.value)
    assert "500" in message
    for value in (SECRET, "hvs.token-secreto", SA_TOKEN):
        assert value not in message
    assert "senha" not in repr(client)


async def test_the_dev_token_file_replaces_the_login(tmp_path: Path) -> None:
    bao = _bao()
    token = bao.issue_token()
    token_file = tmp_path / "token-bao"
    token_file.write_text(token)
    settings = BaoSettings(address=ADDRESS, token_file=token_file, dev=True)
    client = OpenBaoClient(settings, transport=httpx2.ASGITransport(app=bao.app))
    try:
        assert await client.read_kv("rapa", "empresas/x/portal") is not None
    finally:
        await client.aclose()
    assert bao.logins == 0


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"address": "http://openbao.cofre.test:8200", "role": ROLE}, "https"),
        ({"address": ADDRESS, "role": ROLE, "token_file": Path("/tmp/t")}, "RAPA_DEV"),
        ({"address": ADDRESS}, "RAPA_BAO_PAPEL"),
        ({"address": ADDRESS + "/v1", "role": ROLE}, "esquema, host e porta"),
        ({"address": ADDRESS, "role": ROLE, "kv_mount": "../sys"}, "montagem"),
    ],
)
def test_settings_fail_closed(changes: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        BaoSettings.model_validate(changes)
    # No próprio computador (testes, bao server -dev), http vale.
    BaoSettings(address="http://127.0.0.1:8200", role=ROLE)


async def test_readiness_is_ready_only_when_unsealed(tmp_path: Path) -> None:
    bao = _bao()
    client = bao_client(bao, tmp_path)
    check = BaoReadiness(client)
    try:
        assert check.name == "openbao"
        assert await check.ready() is True
        bao.sealed = True
        assert await check.ready() is False
    finally:
        await client.aclose()

    def down(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("recusada")

    settings = BaoSettings(address=ADDRESS, role=ROLE)
    offline = OpenBaoClient(settings, transport=httpx2.MockTransport(down))
    assert await BaoReadiness(offline).ready() is False
    await offline.aclose()


def _free_port() -> int:
    return free_port()


def _tls_bao(bao: FakeBao, cert: Path | None, key: Path | None) -> Any:
    """O OpenBao falso num servidor de verdade (com TLS, se o certificado vier)."""
    return serve(bao.app, cert, key)


async def test_tls_is_checked_with_the_configured_ca(tmp_path: Path) -> None:
    ca, cert, key = make_tls(tmp_path)
    bao = _bao()
    with _tls_bao(bao, cert, key) as port:
        address = f"https://127.0.0.1:{port}"
        trusted = OpenBaoClient(
            BaoSettings(
                address=address, role=ROLE, ca_file=ca, sa_token_file=sa_token_file(tmp_path)
            )
        )
        try:
            assert await trusted.read_kv("rapa", "empresas/x/portal") is not None
        finally:
            await trusted.aclose()
        # Sem o CA (só os do sistema), o certificado do servidor não vale: nada é enviado.
        untrusted = OpenBaoClient(
            BaoSettings(address=address, role=ROLE, sa_token_file=sa_token_file(tmp_path))
        )
        logins = bao.logins
        try:
            with pytest.raises(VaultUnavailable):
                await untrusted.read_kv("rapa", "empresas/x/portal")
        finally:
            await untrusted.aclose()
        assert bao.logins == logins


_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


async def test_the_proxy_of_the_environment_is_never_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Com HTTP(S)_PROXY no ambiente (o proxy de saída do pod), o cliente vai direto ao
    OpenBao: o token e os segredos nunca passam por um proxy."""
    closed = _free_port()  # um "proxy" que não existe: usá-lo seria um erro de conexão
    for name in _PROXY_VARS:
        monkeypatch.setenv(name, f"http://127.0.0.1:{closed}")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    bao = _bao()
    with _tls_bao(bao, None, None) as port:
        settings = BaoSettings(
            address=f"http://127.0.0.1:{port}", role=ROLE, sa_token_file=sa_token_file(tmp_path)
        )
        client = OpenBaoClient(settings)
        try:
            assert await client.read_kv("rapa", "empresas/x/portal") is not None
        finally:
            await client.aclose()
    assert bao.logins == 1


@contextmanager
def _silent_server() -> Iterator[int]:
    """Aceita a conexão e nunca responde (um OpenBao travado)."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    accepted: list[socket.socket] = []
    stop = threading.Event()

    def accept() -> None:
        server.settimeout(0.1)
        while not stop.is_set():
            try:
                accepted.append(server.accept()[0])
            except TimeoutError:
                continue
            except OSError:
                return

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    try:
        yield int(server.getsockname()[1])
    finally:
        stop.set()
        thread.join(timeout=5)
        for conn in accepted:
            conn.close()
        server.close()


async def test_short_timeouts_give_up_on_a_stuck_openbao(tmp_path: Path) -> None:
    with _silent_server() as port:
        settings = BaoSettings(
            address=f"http://127.0.0.1:{port}",
            role=ROLE,
            sa_token_file=sa_token_file(tmp_path),
            timeout_s=0.3,
            connect_timeout_s=0.2,
        )
        client = OpenBaoClient(settings)
        started = time.monotonic()
        try:
            with pytest.raises(VaultUnavailable, match="Timeout"):
                await client.read_kv("rapa", "empresas/x/portal")
        finally:
            await client.aclose()
        assert time.monotonic() - started < 2.5  # o padrão do httpx (5 s) não vale
    # Os prazos de conexão e de leitura são os configurados (padrão: 3 s e 5 s).
    default = OpenBaoClient(BaoSettings(address=ADDRESS, role=ROLE))
    timeout = default._http().timeout
    assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (3.0, 5.0, 5.0, 5.0)
    assert default._http().follow_redirects is False
    await default.aclose()
