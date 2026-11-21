"""Guarda da saída do navegador da sessão: o proxy SOCKS5 que recusa a rede interna."""

import asyncio
import contextlib
import itertools
import socket
import struct
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from rapa.fixtures.server import HOST_RESOLVER_ARG
from rapa.session.egress import (
    BROWSER_NETWORK_ARGS,
    EgressGuard,
    HostRule,
    blocked_address,
    internal_host,
    parse_host_rules,
)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "127.9.8.7",
        "::1",
        "::ffff:127.0.0.1",
        "169.254.169.254",
        "169.254.170.2",
        "fe80::1",
        "fe80::1%eth0",
        "0.0.0.0",
        "0.1.2.3",
        "::",
        "224.0.0.251",
        "ff02::1",
        "fd00:ec2::254",
        "fd00:ec2::23",
        "100.100.100.200",
        "::ffff:169.254.169.254",
        "não é endereço",
    ],
)
def test_internal_addresses_are_blocked(address: str) -> None:
    assert blocked_address(address)


@pytest.mark.parametrize(
    "address", ["93.184.216.34", "2606:2800:220:1::1", "10.0.0.5", "192.168.1.10", "fd12::1"]
)
def test_public_and_private_addresses_pass(address: str) -> None:
    # As redes privadas ficam abertas (portais de intranet): a NetworkPolicy do deploy as fecha.
    assert not blocked_address(address)


@pytest.mark.parametrize(
    ("host", "internal"),
    [
        ("localhost", True),
        ("LOCALHOST.", True),
        ("api.localhost", True),
        ("[::1]", True),
        ("127.0.0.1", True),
        ("169.254.169.254", True),
        ("portal.exemplo.gov.br", False),
        ("10.1.2.3", False),
    ],
)
def test_internal_host_without_resolving(host: str, internal: bool) -> None:
    assert internal_host(host) is internal


def test_host_rules_follow_chromium() -> None:
    # O último --host-resolver-rules vale; aspas (como o Playwright as põe) caem.
    args = (
        "--host-resolver-rules=MAP * ~NOTFOUND",
        '--host-resolver-rules="MAP *.rapa.test 127.0.0.1, EXCLUDE x.rapa.test, MAP a.b 1.2.3.4:81"',
    )
    assert parse_host_rules(args) == [
        HostRule("*.rapa.test", "127.0.0.1"),
        HostRule("x.rapa.test", None),
        HostRule("a.b", "1.2.3.4:81"),
    ]
    assert parse_host_rules(("--headless",)) == []


def test_udp_is_kept_out_of_the_browser() -> None:
    assert "--disable-quic" in BROWSER_NETWORK_ARGS
    assert "--webrtc-ip-handling-policy=disable_non_proxied_udp" in BROWSER_NETWORK_ARGS


# -- o proxy de verdade --------------------------------------------------------------------


@asynccontextmanager
async def echo_server() -> AsyncIterator[int]:
    """Um serviço em 127.0.0.1 que devolve o que recebe e fecha quando o outro lado fecha."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(Exception):
            while data := await reader.read(1024):
                writer.write(data)
                await writer.drain()
            writer.write(b"<fim>")
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield int(server.sockets[0].getsockname()[1])
    finally:
        server.close()
        server.close_clients()


@asynccontextmanager
async def running_guard(guard: EgressGuard) -> AsyncIterator[int]:
    port = await guard.start()
    try:
        yield port
    finally:
        await guard.close()


async def _closed(reader: asyncio.StreamReader) -> bool:
    """O outro lado fechou (EOF ou conexão derrubada) sem mandar mais nada?"""
    try:
        return await asyncio.wait_for(reader.read(), 5) == b""
    except ConnectionResetError:
        return True


def _request(command: int, host: str, port: int) -> bytes:
    try:
        packed = socket.inet_pton(socket.AF_INET, host)
        address = b"\x01" + packed
    except OSError:
        try:
            address = b"\x04" + socket.inet_pton(socket.AF_INET6, host)
        except OSError:
            raw = host.encode()
            address = b"\x03" + bytes([len(raw)]) + raw
    return bytes([5, command, 0]) + address + struct.pack(">H", port)


async def _socks(
    proxy: int, host: str, port: int, *, command: int = 1
) -> tuple[int, asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy)
    writer.write(b"\x05\x01\x00")
    assert await reader.readexactly(2) == b"\x05\x00"
    writer.write(_request(command, host, port))
    reply = await asyncio.wait_for(reader.readexactly(10), 15)
    assert reply[0] == 5
    return reply[1], reader, writer


async def test_loopback_metadata_and_localhost_are_refused_by_the_rule() -> None:
    async with echo_server() as target:
        refused: list[str] = []
        guard = EgressGuard(on_refused=refused.append)
        async with running_guard(guard) as proxy:
            for host in ("127.0.0.1", "::1", "localhost", "169.254.169.254", "0.0.0.0"):
                code, _, writer = await _socks(proxy, host, target)
                writer.close()
                assert code == 0x02, host
        assert guard.refused == 5 and guard.allowed == 0
        assert refused == ["127.0.0.1", "::1", "localhost", "169.254.169.254", "0.0.0.0"]


async def test_a_name_resolving_inside_is_refused_and_the_checked_address_is_used() -> None:
    answers = {
        "rebind.exemplo": ["93.184.216.34", "127.0.0.1"],  # uma resposta interna basta
        "metadata.exemplo": ["169.254.169.254"],
        "mapped.exemplo": ["::ffff:127.0.0.1"],
    }

    async def resolver(name: str) -> list[str]:
        return answers[name]

    guard = EgressGuard(resolver=resolver)
    for name in answers:
        assert await guard.resolve(name, 80) == [], name
    answers["publico.exemplo"] = ["93.184.216.34", "93.184.216.34", "2606:2800:220:1::1"]
    assert await guard.resolve("publico.exemplo", 443) == [
        ("93.184.216.34", 443),
        ("2606:2800:220:1::1", 443),
    ]

    async def broken(name: str) -> list[str]:
        raise socket.gaierror("sem nome")

    assert await EgressGuard(resolver=broken).resolve("sumiu.exemplo", 80) == []


async def test_operator_mapped_names_are_piped_both_ways_with_half_close() -> None:
    async with echo_server() as target:
        guard = EgressGuard.from_browser_args((HOST_RESOLVER_ARG,))
        async with running_guard(guard) as proxy:
            code, reader, writer = await _socks(proxy, "portal.rapa.test", target)
            assert code == 0x00
            writer.write(b"ola")
            await writer.drain()
            assert await reader.readexactly(3) == b"ola"
            # O navegador fecha a escrita: a resposta que falta ainda chega.
            writer.write_eof()
            assert await asyncio.wait_for(reader.read(), 5) == b"<fim>"
            writer.close()
            # EXCLUDE e ~NOTFOUND valem como no Chromium.
            rules = (
                "--host-resolver-rules=MAP *.rapa.test 127.0.0.1, EXCLUDE fora.rapa.test,"
                " MAP nada.exemplo ~NOTFOUND"
            )
            guard.rules = tuple(parse_host_rules((rules,)))
            assert await guard.resolve("fora.rapa.test", target) == []
            assert await guard.resolve("nada.exemplo", target) == []
        assert guard.active == 0


async def test_only_connect_without_authentication_is_accepted() -> None:
    guard = EgressGuard.from_browser_args((HOST_RESOLVER_ARG,))
    async with running_guard(guard) as proxy:
        # Só usuário e senha: nenhum método aceitável.
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy)
        writer.write(b"\x05\x01\x02")
        assert await reader.readexactly(2) == b"\x05\xff"
        assert await reader.read() == b""
        writer.close()
        # BIND e UDP ASSOCIATE: comando não aceito.
        for command in (2, 3):
            code, _, writer = await _socks(proxy, "portal.rapa.test", 80, command=command)
            assert code == 0x07
            writer.close()
        # Tipo de endereço desconhecido.
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy)
        writer.write(b"\x05\x01\x00" + b"\x05\x01\x00\x09")
        assert await reader.readexactly(2) == b"\x05\x00"
        assert (await reader.readexactly(10))[1] == 0x08
        writer.close()
        # SOCKS4 não é aceito.
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy)
        writer.write(b"\x04\x01\x00\x50\x7f\x00\x00\x01\x00")
        assert await reader.readexactly(2) == b"\x05\xff"
        writer.close()


async def test_a_closed_port_answers_connection_refused() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed = probe.getsockname()[1]
    guard = EgressGuard.from_browser_args((HOST_RESOLVER_ARG,))
    async with running_guard(guard) as proxy:
        code, _, writer = await _socks(proxy, "portal.rapa.test", closed)
        writer.close()
    assert code == 0x05


async def test_a_silent_client_is_dropped_after_the_handshake_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("rapa.session.egress.HANDSHAKE_TIMEOUT_S", 0.3)
    guard = EgressGuard()
    async with running_guard(guard) as proxy:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy)
        writer.write(b"\x05")  # metade da saudação, e nada mais
        assert await asyncio.wait_for(reader.read(), 5) == b""
        writer.close()
        assert guard.active == 0


async def test_connections_are_bounded_and_closed_with_the_guard() -> None:
    async with echo_server() as target:
        guard = EgressGuard.from_browser_args((HOST_RESOLVER_ARG,), on_refused=None)
        guard.max_connections = 2
        proxy = await guard.start()
        open_ones = [await _socks(proxy, "portal.rapa.test", target) for _ in range(2)]
        assert [code for code, *_ in open_ones] == [0, 0]
        assert guard.active == 2
        # A terceira ao mesmo tempo é derrubada sem resposta.
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy)
        writer.write(b"\x05\x01\x00")
        assert await _closed(reader)
        writer.close()
        # Fechar a guarda derruba as conexões abertas (o fim da sessão).
        await guard.close()
        for _, reader, writer in open_ones:
            assert await _closed(reader)
            writer.close()
        assert guard.active == 0


def _own_addresses() -> list[str]:
    """Endereços IPv4 desta máquina fora do loopback (o IP do pod ou da máquina da API)."""
    found: list[str] = []
    with contextlib.suppress(OSError):
        lines = Path("/proc/net/fib_trie").read_text().splitlines()
        for before, line in itertools.pairwise(lines):
            address = before.strip().removeprefix("|-- ")
            if "/32 host LOCAL" in line and not address.startswith("127."):
                found.append(address)
    return sorted(set(found))


def test_the_machines_own_addresses_are_blocked() -> None:
    own = _own_addresses()
    if not own:
        pytest.skip("esta máquina não tem endereço fora do loopback")
    # A API num contêiner escuta em 0.0.0.0: o endereço da própria máquina chega nela.
    assert all(blocked_address(address) for address in own)
    assert internal_host(own[0])
    # Um endereço vizinho, de outra máquina da mesma rede, continua aberto (intranet).
    assert not blocked_address("10.255.255.254")


@pytest.mark.parametrize(
    "address",
    ["::127.0.0.1", "64:ff9b::7f00:1", "2002:7f00:1::", "64:ff9b::a9fe:a9fe", "::ffff:0.0.0.1"],
)
def test_an_ipv6_form_of_a_blocked_ipv4_is_blocked(address: str) -> None:
    # O IPv4 dentro do IPv6 (compatível, NAT64, 6to4 ou mapeado) vale como o próprio IPv4.
    assert blocked_address(address)


def test_an_ipv6_form_of_a_public_ipv4_is_not_blocked() -> None:
    assert not blocked_address("64:ff9b::808:808")
