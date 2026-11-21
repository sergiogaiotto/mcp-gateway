"""Guarda da saída do navegador da sessão: um proxy SOCKS5 local que recusa a rede interna.

O Chromium da sessão roda na máquina do plano de controle (o lançador local) ou num
pod do cluster. Sem guarda, a pessoa que demonstra poderia abrir a própria máquina
(``http://127.0.0.1:<porta da API>``, o banco, o canal de outras sessões), o
metadata da nuvem (``169.254.169.254``, com as credenciais da instância) ou
qualquer serviço que escute só em ``localhost``, e ver a página pela tela ao vivo.
O ``route`` do Playwright não basta: ele não é chamado nos saltos de um
redirecionamento.

Por isso o hospedeiro abre o Chromium com ``--proxy-server=socks5://127.0.0.1:<porta>``
desta guarda. O Playwright acrescenta ``<-loopback>`` (o loopback também passa pelo
proxy) e, sem regras próprias do operador, ``MAP * ~NOTFOUND`` (o navegador não
resolve nomes sozinho). Toda conexão TCP do navegador (navegação, redirecionamentos,
iframes, workers, service workers, websockets, downloads, o TCP do WebRTC) chega
aqui com o nome do destino; a guarda:

1. resolve o nome ela mesma e recusa (resposta SOCKS 0x02, "proibida pela regra") se
   qualquer endereço for da própria máquina (loopback, ``localhost``), link-local
   (o metadata da nuvem), ``0.0.0.0/8``, não especificado, multicast, os endereços
   de metadata fora do link-local (``fd00:ec2::/32`` da AWS, ``100.100.100.200``) ou
   a forma IPv4 mapeada em IPv6 de um deles;
2. conecta exatamente ao endereço que conferiu (uma troca de DNS no meio não muda o
   destino);
3. só aceita o método sem autenticação e o comando CONNECT (BIND e UDP ASSOCIATE
   recebem 0x07), com prazo para o aperto de mão, para a resolução e para a conexão,
   e no máximo :data:`MAX_CONNECTIONS` conexões ao mesmo tempo.

O UDP fica de fora do proxy: o hospedeiro desliga o QUIC (``--disable-quic``) e
força o WebRTC a só usar o que passa pelo proxy
(``--force-webrtc-ip-handling-policy=disable_non_proxied_udp``).

Os nomes que o operador mapeia explicitamente em ``--host-resolver-rules`` (``MAP
nome endereço``; os portais de teste em ``*.rapa.test``) valem como mapeados, mesmo
para o loopback: é configuração do operador, nunca da pessoa.

**Resíduo conhecido:** as redes privadas (10/8, 172.16/12, 192.168/16, fc00::/7)
continuam abertas, porque os portais de intranet são o caso de uso. As sub-redes do
banco, da API do Kubernetes e dos outros serviços do plano de controle precisam ser
fechadas pela NetworkPolicy (ou pelo proxy de saída) do deploy.
"""

import asyncio
import contextlib
import fnmatch
import ipaddress
import logging
import socket
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

__all__ = [
    "BROWSER_NETWORK_ARGS",
    "MAX_CONNECTIONS",
    "EgressGuard",
    "HostRule",
    "blocked_address",
    "internal_host",
    "parse_host_rules",
]

logger = logging.getLogger("rapa.session.egress")

#: Conexões do navegador ao mesmo tempo (o Chromium usa até 256 por perfil).
MAX_CONNECTIONS = 256
#: Prazo para o navegador mandar a saudação e o pedido SOCKS.
HANDSHAKE_TIMEOUT_S = 10.0
#: Prazo da resolução de um nome.
RESOLVE_TIMEOUT_S = 10.0
#: Prazo para abrir a conexão com o destino conferido.
CONNECT_TIMEOUT_S = 30.0
_CHUNK = 65536

#: Argumentos do Chromium que tiram o UDP do caminho (o proxy só leva TCP). Vêm depois
#: dos argumentos do operador, para não serem desfeitos por eles.
BROWSER_NETWORK_ARGS = (
    "--disable-quic",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
)

# Respostas SOCKS5 (RFC 1928).
_GENERAL_FAILURE = 0x01
_NOT_ALLOWED = 0x02
_HOST_UNREACHABLE = 0x04
_REFUSED = 0x05
_COMMAND_NOT_SUPPORTED = 0x07
_ADDRESS_NOT_SUPPORTED = 0x08

_THIS_NETWORK = ipaddress.ip_network("0.0.0.0/8")
#: Metadata da nuvem fora do link-local: o IMDS e o Pod Identity da AWS em IPv6
#: (``fd00:ec2::254`` e ``fd00:ec2::23``) e o metadata da Alibaba Cloud.
_METADATA_NETWORKS = (
    ipaddress.ip_network("fd00:ec2::/32"),
    ipaddress.ip_network("100.100.100.200/32"),
)

type Resolver = Callable[[str], Awaitable[list[str]]]


def blocked_address(address: str) -> bool:
    """O endereço é da própria máquina, link-local, de metadata ou sem destino válido?"""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return True
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if isinstance(ip, ipaddress.IPv4Address) and ip in _THIS_NETWORK:
        return True
    if any(ip in network for network in _METADATA_NETWORKS):
        return True
    return ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast


def _is_localhost(name: str) -> bool:
    return name == "localhost" or name.endswith(".localhost")


def internal_host(host: str) -> bool:
    """O nome ou endereço literal aponta, sem resolver, para a rede que a guarda recusa?"""
    name = host.strip("[]").rstrip(".").lower()
    if _is_localhost(name):
        return True
    try:
        ipaddress.ip_address(name.split("%", 1)[0])
    except ValueError:
        return False
    return blocked_address(name)


@dataclass(frozen=True, slots=True)
class HostRule:
    """Uma regra ``MAP padrão destino`` ou ``EXCLUDE padrão`` do ``--host-resolver-rules``."""

    pattern: str
    #: Endereço (ou nome) mapeado, com a porta se houver; None numa regra EXCLUDE.
    target: str | None


def parse_host_rules(browser_args: Sequence[str]) -> list[HostRule]:
    """As regras do último ``--host-resolver-rules`` (como no Chromium, o último vale)."""
    value: str | None = None
    for arg in browser_args:
        if arg.startswith("--host-resolver-rules="):
            value = arg.split("=", 1)[1]
    if value is None:
        return []
    rules: list[HostRule] = []
    for raw in value.strip().strip("\"'").split(","):
        parts = raw.split()
        if len(parts) == 3 and parts[0].upper() == "MAP":
            rules.append(HostRule(parts[1].lower(), parts[2]))
        elif len(parts) == 2 and parts[0].upper() == "EXCLUDE":
            rules.append(HostRule(parts[1].lower(), None))
    return rules


def _split_target(target: str, port: int) -> tuple[str, int]:
    """``destino[:porta]`` de uma regra MAP (a porta do pedido se a regra não tiver)."""
    if target.startswith("["):
        host, _, rest = target[1:].partition("]")
        return host, int(rest[1:]) if rest.startswith(":") and rest[1:].isdigit() else port
    if target.count(":") == 1:
        host, _, raw_port = target.partition(":")
        if raw_port.isdigit():
            return host, int(raw_port)
    return target, port


async def _system_resolver(name: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(name, None, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


class EgressGuard:
    """Proxy SOCKS5 local do navegador da sessão (ver o módulo)."""

    def __init__(
        self,
        rules: Sequence[HostRule] = (),
        *,
        resolver: Resolver | None = None,
        on_refused: Callable[[str], None] | None = None,
        max_connections: int = MAX_CONNECTIONS,
    ) -> None:
        self.rules = tuple(rules)
        self._resolve_name = resolver or _system_resolver
        self.on_refused = on_refused
        self.max_connections = max_connections
        self._server: asyncio.Server | None = None
        self._clients: set[asyncio.StreamWriter] = set()
        #: Contagens (para diagnóstico e testes; nunca os destinos).
        self.allowed = 0
        self.refused = 0

    @classmethod
    def from_browser_args(
        cls, browser_args: Sequence[str], *, on_refused: Callable[[str], None] | None = None
    ) -> EgressGuard:
        """A guarda com as regras MAP do operador nos argumentos do navegador."""
        return cls(parse_host_rules(browser_args), on_refused=on_refused)

    @property
    def active(self) -> int:
        return len(self._clients)

    async def start(self, host: str = "127.0.0.1") -> int:
        """Abre a guarda numa porta livre do loopback e devolve a porta."""
        self._server = await asyncio.start_server(self._client, host, 0)
        for sock in self._server.sockets:
            name = sock.getsockname()
            if isinstance(name, tuple) and len(name) >= 2:
                return int(name[1])
        raise RuntimeError("a guarda da saída não abriu porta")  # pragma: no cover

    async def close(self) -> None:
        """Fecha a porta e derruba as conexões abertas (o navegador já fechou ou vai fechar)."""
        server, self._server = self._server, None
        if server is None:
            return
        server.close()
        for writer in list(self._clients):
            with contextlib.suppress(Exception):
                writer.transport.abort()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(server.wait_closed(), timeout=5)

    # -- regras ------------------------------------------------------------------

    def _rule_for(self, name: str) -> HostRule | None:
        """A regra MAP que vale para o nome (como no Chromium, EXCLUDE vence)."""
        if any(r.target is None and fnmatch.fnmatchcase(name, r.pattern) for r in self.rules):
            return None
        for rule in self.rules:
            if rule.target is not None and fnmatch.fnmatchcase(name, rule.pattern):
                return rule
        return None

    async def resolve(self, host: str, port: int) -> list[tuple[str, int]]:
        """Os destinos conferidos para ``host:port``; vazio se a guarda recusa."""
        name = host.strip("[]").rstrip(".").lower()
        if not name:
            return []
        rule = self._rule_for(name)
        if rule is not None and rule.target is not None:
            if rule.target.upper() == "~NOTFOUND":
                return []
            return [_split_target(rule.target, port)]  # configuração do operador
        if _is_localhost(name):
            return []
        try:
            ipaddress.ip_address(name.split("%", 1)[0])
        except ValueError:
            pass
        else:
            return [] if blocked_address(name) else [(name, port)]
        try:
            addresses = await asyncio.wait_for(self._resolve_name(name), RESOLVE_TIMEOUT_S)
        except OSError, TimeoutError, UnicodeError:
            return []
        # Um nome com qualquer resposta interna é recusado inteiro (sem escolher a "boa").
        if not addresses or any(blocked_address(a) for a in addresses):
            return []
        unique = list(dict.fromkeys(addresses))
        return [(address, port) for address in unique]

    # -- protocolo -----------------------------------------------------------------

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if len(self._clients) >= self.max_connections:
            writer.transport.abort()
            return
        self._clients.add(writer)
        upstream: asyncio.StreamWriter | None = None
        try:
            request = await asyncio.wait_for(self._handshake(reader, writer), HANDSHAKE_TIMEOUT_S)
            if request is None:
                return
            host, port = request
            targets = await self.resolve(host, port)
            if not targets:
                self.refused += 1
                await _reply(writer, _NOT_ALLOWED)
                if self.on_refused is not None:
                    with contextlib.suppress(Exception):
                        self.on_refused(host)
                return
            opened = await self._connect(targets)
            if isinstance(opened, int):
                await _reply(writer, opened)
                return
            up_reader, upstream = opened
            self.allowed += 1
            await _reply(writer, 0x00)
            await _splice(reader, writer, up_reader, upstream)
        except OSError, TimeoutError, asyncio.IncompleteReadError, ValueError:
            return
        finally:
            self._clients.discard(writer)
            for stream in (upstream, writer):
                if stream is not None:
                    with contextlib.suppress(Exception):
                        stream.close()

    async def _handshake(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> tuple[str, int] | None:
        version, count = await reader.readexactly(2)
        methods = await reader.readexactly(count)
        if version != 5 or 0x00 not in methods:
            writer.write(b"\x05\xff")  # nenhum método aceitável
            await writer.drain()
            return None
        writer.write(b"\x05\x00")
        await writer.drain()
        version, command, _, kind = await reader.readexactly(4)
        if version != 5:
            return None
        if kind == 1:
            host = socket.inet_ntop(socket.AF_INET, await reader.readexactly(4))
        elif kind == 4:
            host = socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
        elif kind == 3:
            size = (await reader.readexactly(1))[0]
            raw = await reader.readexactly(size)
            try:
                host = raw.decode("ascii")
            except UnicodeDecodeError:
                await _reply(writer, _NOT_ALLOWED)
                return None
        else:
            await _reply(writer, _ADDRESS_NOT_SUPPORTED)
            return None
        port = int.from_bytes(await reader.readexactly(2), "big")
        if command != 1:
            await _reply(writer, _COMMAND_NOT_SUPPORTED)  # BIND e UDP ASSOCIATE, nunca
            return None
        if port == 0:
            await _reply(writer, _NOT_ALLOWED)
            return None
        return host, port

    async def _connect(
        self, targets: list[tuple[str, int]]
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | int:
        failure = _HOST_UNREACHABLE
        for address, port in targets:
            try:
                return await asyncio.wait_for(
                    asyncio.open_connection(address, port), CONNECT_TIMEOUT_S
                )
            except ConnectionRefusedError:
                failure = _REFUSED
            except OSError, TimeoutError:
                failure = _HOST_UNREACHABLE
        return failure if targets else _GENERAL_FAILURE


async def _reply(writer: asyncio.StreamWriter, code: int) -> None:
    writer.write(bytes((0x05, code, 0x00, 0x01)) + bytes(6))
    with contextlib.suppress(Exception):
        await writer.drain()


async def _splice(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    up_reader: asyncio.StreamReader,
    up_writer: asyncio.StreamWriter,
) -> None:
    """Copia nos dois sentidos até os dois lados fecharem.

    Um fim limpo de um lado (EOF) só fecha a escrita do outro (``write_eof``); um erro
    num sentido encerra os dois.
    """
    tasks = [
        asyncio.ensure_future(_pump(reader, up_writer)),
        asyncio.ensure_future(_pump(up_reader, writer)),
    ]
    try:
        pending: set[asyncio.Future[bool]] = set(tasks)
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            if any(task.cancelled() or task.exception() or not task.result() for task in done):
                break
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> bool:
    """Copia ``reader`` para ``writer``; True num fim limpo, False num erro."""
    try:
        while data := await reader.read(_CHUNK):
            writer.write(data)
            await writer.drain()
    except OSError:
        return False
    with contextlib.suppress(OSError, RuntimeError):
        if writer.can_write_eof():
            writer.write_eof()
            return True
    return False
