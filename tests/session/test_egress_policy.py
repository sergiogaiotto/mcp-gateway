"""Guarda da saída com lista de hosts, rede interna fechada, redes permitidas e proxy de saída."""

import asyncio
import contextlib
import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import pytest

from rapa.session.egress import (
    EgressGuard,
    EgressPolicy,
    Route,
    UpstreamProxy,
    parse_networks,
    without_proxy_switches,
)
from tests.session.test_egress import _socks, echo_server, running_guard

PUBLIC = "93.184.216.34"


def _resolver(answers: dict[str, list[str]]) -> Callable[[str], Awaitable[list[str]]]:
    async def resolve(name: str) -> list[str]:
        if name not in answers:
            raise socket.gaierror("sem nome")
        return answers[name]

    return resolve


async def test_a_host_list_refuses_other_names_and_literal_addresses() -> None:
    answers = {"portal.exemplo": [PUBLIC], "outro.exemplo": ["93.184.216.35"]}
    guard = EgressGuard(resolver=_resolver(answers), allowed_hosts=["Portal.Exemplo"])
    assert await guard.resolve("PORTAL.exemplo.", 443) == [(PUBLIC, 443)]
    # Fora da lista do robô: recusado, mesmo sendo da internet; e nenhum endereço literal.
    assert await guard.route("outro.exemplo", 443) is None
    assert await guard.route(PUBLIC, 443) is None
    assert await guard.route("[2606:2800:220:1::1]", 443) is None
    # A sessão de gravação não tem lista.
    session = EgressGuard(resolver=_resolver(answers))
    assert await session.resolve("outro.exemplo", 443) == [("93.184.216.35", 443)]


async def test_an_operator_mapped_name_still_needs_to_be_on_the_list() -> None:
    rules = ("--host-resolver-rules=MAP *.rapa.test 127.0.0.1",)
    guard = EgressGuard.from_browser_args(rules, allowed_hosts=["portal-v1.rapa.test"])
    assert await guard.resolve("portal-v1.rapa.test", 8080) == [("127.0.0.1", 8080)]
    assert await guard.route("fora.rapa.test", 8080) is None


@pytest.mark.parametrize(
    "answer",
    [
        ["10.1.2.3"],
        ["172.20.0.9"],
        ["192.168.0.5"],
        ["100.64.0.9"],  # a rede compartilhada (CGNAT), que o EKS usa em VPCs
        ["fd12::1"],
        ["192.0.2.10"],  # documentação: não é da internet
        [PUBLIC, "10.0.0.7"],  # uma resposta interna basta
        ["::ffff:10.1.2.3"],
    ],
)
async def test_with_a_policy_a_name_that_points_inside_is_refused(answer: list[str]) -> None:
    resolver = _resolver({"portal.exemplo": answer})
    guarded = EgressGuard(
        resolver=resolver, allowed_hosts=["portal.exemplo"], policy=EgressPolicy()
    )
    assert await guarded.route("portal.exemplo", 443) is None
    # Sem política (a sessão local no desenvolvimento), as redes privadas continuam abertas.
    if answer == ["10.1.2.3"]:
        assert await EgressGuard(resolver=resolver).resolve("portal.exemplo", 443) != []


async def test_an_allowed_internal_network_passes_and_nothing_else_does() -> None:
    policy = EgressPolicy.parse("10.20.0.0/16, fd12::/64")
    answers = {
        "intranet.exemplo": ["10.20.5.6"],
        "vizinha.exemplo": ["10.21.0.1"],
        "v6.exemplo": ["fd12::8"],
        "publico.exemplo": [PUBLIC],
        "mapeado.exemplo": ["::ffff:10.20.5.6"],
    }
    guard = EgressGuard(resolver=_resolver(answers), policy=policy)
    assert await guard.resolve("intranet.exemplo", 443) == [("10.20.5.6", 443)]
    assert await guard.resolve("v6.exemplo", 443) == [("fd12::8", 443)]
    assert await guard.resolve("publico.exemplo", 443) == [(PUBLIC, 443)]
    assert await guard.resolve("mapeado.exemplo", 443) == [("::ffff:10.20.5.6", 443)]
    assert await guard.route("vizinha.exemplo", 443) is None
    assert await guard.resolve("10.20.9.9", 80) == [("10.20.9.9", 80)]
    assert await guard.route("10.21.9.9", 80) is None
    # A própria máquina, o link-local e o metadata nunca abrem.
    for literal in ("127.0.0.1", "169.254.169.254", "0.0.0.0", "::1"):
        assert await guard.route(literal, 80) is None, literal


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("10.20.1.0/16", "formato exato"),
        ("não é rede", "rede interna inválida"),
        ("127.0.0.0/8", "sempre recusa"),
        ("0.0.0.0/0", "sempre recusa"),
        ("169.254.0.0/16", "sempre recusa"),
        ("::ffff:0:0/96", "sempre recusa"),
        ("fd00:ec2::/32", "sempre recusa"),
        ("::/0", "sempre recusa"),
    ],
)
def test_internal_networks_are_checked(raw: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_networks(raw)


def test_internal_networks_parse_from_the_environment_format() -> None:
    nets = parse_networks(" 10.20.0.0/16,192.168.5.0/24,, 10.20.0.0/16 ")
    assert [str(n) for n in nets] == ["10.20.0.0/16", "192.168.5.0/24"]
    assert parse_networks("") == ()


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("https://proxy.interno:3128", "http://host:porta"),
        # Com usuário e senha (ver test_egress_proxy_auth), os erros também não os repetem.
        ("http://usuario:Tr0ub4dor@proxy.interno", "informe a porta"),
        ("https://usuario:Tr0ub4dor@proxy.interno:3128", "http://host:porta"),
        ("http://proxy.interno", "informe a porta"),
        ("http://proxy.interno:3128/caminho", "sem caminho"),
        ("http://proxy.interno:porta", "http://host:porta"),
    ],
)
def test_upstream_proxy_is_only_http_host_and_port(url: str, message: str) -> None:
    with pytest.raises(ValueError, match=message) as caught:
        UpstreamProxy.parse(url)
    assert "Tr0ub4dor" not in str(caught.value)


def test_upstream_proxy_round_trip() -> None:
    proxy = UpstreamProxy.parse("http://proxy.interno:3128/")
    assert (proxy.host, proxy.port, proxy.url) == (
        "proxy.interno",
        3128,
        "http://proxy.interno:3128",
    )
    assert UpstreamProxy.parse("http://[fd12::3]:8080").url == "http://[fd12::3]:8080"
    policy = EgressPolicy.parse(["10.20.0.0/16"], "http://proxy.interno:3128")
    assert policy.worker_args() == [
        "--rede-interna-permitida=10.20.0.0/16",
        "--proxy-saida=http://proxy.interno:3128",
    ]


def test_operator_proxy_switches_are_removed() -> None:
    args = [
        "--proxy-server=http://fora:1",
        "--no-proxy-server",
        "--proxy-bypass-list=*",
        "--proxy-pac-url=http://x/p.pac",
        "--proxy-auto-detect",
        "--host-resolver-rules=MAP a 1.2.3.4",
        "--lang=pt-BR",
    ]
    assert without_proxy_switches(args) == ["--host-resolver-rules=MAP a 1.2.3.4", "--lang=pt-BR"]


# -- o proxy de saída de verdade -----------------------------------------------------------


@asynccontextmanager
async def connect_proxy(
    target_port: int, *, status: int = 200
) -> AsyncIterator[tuple[int, list[str]]]:
    """Um proxy HTTP CONNECT de teste: anota o pedido e liga ao serviço local ``target_port``."""
    seen: list[str] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(Exception):
            head = await reader.readuntil(b"\r\n\r\n")
            seen.append(head.split(b"\r\n", 1)[0].decode())
            if status != 200:
                writer.write(f"HTTP/1.1 {status} Recusado\r\nContent-Length: 0\r\n\r\n".encode())
                await writer.drain()
                writer.close()
                return
            up_reader, up_writer = await asyncio.open_connection("127.0.0.1", target_port)
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()

            async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
                with contextlib.suppress(Exception):
                    while data := await src.read(1024):
                        dst.write(data)
                        await dst.drain()
                    dst.write_eof()

            await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield int(server.sockets[0].getsockname()[1]), seen
    finally:
        server.close()
        server.close_clients()


async def test_the_upstream_proxy_gets_the_name_and_resolves_it() -> None:
    async with echo_server() as target, connect_proxy(target) as (proxy_port, seen):
        policy = EgressPolicy.parse((), f"http://127.0.0.1:{proxy_port}")
        # O pod não resolve nomes da internet: só o proxy corporativo resolve.
        guard = EgressGuard(resolver=_resolver({}), allowed_hosts=["portal.exemplo"], policy=policy)
        async with running_guard(guard) as port:
            code, reader, writer = await _socks(port, "portal.exemplo", 443)
            assert code == 0x00
            writer.write(b"ola")
            await writer.drain()
            assert await reader.readexactly(3) == b"ola"
            writer.close()
            # Fora da lista, nem chega ao proxy.
            code, _, writer = await _socks(port, "outro.exemplo", 443)
            writer.close()
            assert code == 0x02
        assert seen == ["CONNECT portal.exemplo:443 HTTP/1.1"]
        assert guard.proxied == 1 and guard.refused == 1


async def test_with_the_proxy_internal_names_go_direct_or_are_refused() -> None:
    policy = EgressPolicy.parse("10.20.0.0/16", "http://proxy.interno:3128")
    answers = {
        "intranet.exemplo": ["10.20.5.6"],
        "banco.exemplo": ["10.30.0.4"],
        "publico.exemplo": [PUBLIC],
        "metadata.exemplo": ["169.254.169.254"],
    }
    guard = EgressGuard(resolver=_resolver(answers), policy=policy)
    # A rede interna permitida vai direto, sem o proxy corporativo.
    assert await guard.route("intranet.exemplo", 443) == Route((("10.20.5.6", 443),))
    # A internet vai pelo proxy, com o nome (o proxy resolve).
    assert await guard.route("publico.exemplo", 443) == Route(
        (("publico.exemplo", 443),), via_proxy=True
    )
    assert await guard.route("sem-dns.exemplo", 443) == Route(
        (("sem-dns.exemplo", 443),), via_proxy=True
    )
    assert await guard.route(PUBLIC, 443) == Route(((PUBLIC, 443),), via_proxy=True)
    # Um nome que aponta para outra rede interna, ou para o metadata, nunca vai ao proxy.
    assert await guard.route("banco.exemplo", 443) is None
    assert await guard.route("metadata.exemplo", 443) is None
    assert await guard.route("localhost", 443) is None
    # Um nome com caracteres que não são de nome nunca vira pedido ao proxy.
    assert await guard.route("a b\r\nX: y", 443) is None


@pytest.mark.parametrize(("status", "code"), [(403, 0x02), (407, 0x02), (502, 0x04)])
async def test_a_refusal_of_the_upstream_proxy_reaches_the_browser(status: int, code: int) -> None:
    async with echo_server() as target, connect_proxy(target, status=status) as (proxy_port, _):
        policy = EgressPolicy.parse((), f"http://127.0.0.1:{proxy_port}")
        guard = EgressGuard(resolver=_resolver({}), policy=policy)
        async with running_guard(guard) as port:
            got, _, writer = await _socks(port, "portal.exemplo", 443)
            writer.close()
    assert got == code


async def test_a_literal_address_with_a_zone_never_reaches_the_connect_line() -> None:
    policy = EgressPolicy.parse((), "http://proxy.interno:3128")
    guard = EgressGuard(resolver=_resolver({}), policy=policy)
    # A reprodução da revisão: o que vem depois do "%" iria inteiro para a linha do CONNECT.
    injected = f"{PUBLIC}%x\r\nproxy-authorization: basic abc"
    assert await guard.route(injected, 443) is None
    assert await guard.route(f"{PUBLIC}%eth0", 443) is None
    assert await guard.route("2606:2800:220:1::1%eth0", 443) is None
    # Sem a política (a sessão local no desenvolvimento), também não.
    plain = EgressGuard(resolver=_resolver({}))
    assert await plain.route("2001:db8::1%x\r\nX: y", 443) is None
    # Um endereço literal vai sempre na forma canônica, nunca no texto pedido.
    assert await guard.route("2606:2800:0220:0001:0000:0000:0000:0001", 443) == Route(
        (("2606:2800:220:1::1", 443),), via_proxy=True
    )
    assert await plain.route("[2606:2800:0220:0001::1]", 443) == Route(
        (("2606:2800:220:1::1", 443),)
    )


async def test_the_connect_line_only_takes_a_checked_destination() -> None:
    from rapa.session.egress import _connect_via_proxy

    connected: list[bytes] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(Exception):
            connected.append(await reader.read(1024))
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        proxy = UpstreamProxy("127.0.0.1", int(server.sockets[0].getsockname()[1]))
        for host in ("a\r\nX: y", "a b", "a%x", "", "x" * 300):
            assert await _connect_via_proxy(proxy, host, 443) == 0x02, host
        assert await _connect_via_proxy(proxy, "portal.exemplo", 0) == 0x02
        # Nada chegou ao proxy: a recusa é antes de abrir a conexão.
        await asyncio.sleep(0.05)
        assert connected == []
    finally:
        server.close()
        server.close_clients()
