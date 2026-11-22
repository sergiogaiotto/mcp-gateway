"""Guarda da saída do navegador: um proxy SOCKS5 local que recusa a rede interna.

Serve ao navegador da sessão de gravação (:mod:`rapa.session.host`) e ao da execução
(:mod:`rapa.runner.worker` com ``--guarda-de-saida``). O Chromium roda na máquina do
plano de controle (os lançadores locais) ou num pod do cluster. Sem guarda, a página
poderia abrir a própria máquina (``http://127.0.0.1:<porta da API>``, o banco, o canal
de outras sessões), o metadata da nuvem (``169.254.169.254``, com as credenciais da
instância) ou qualquer serviço que escute só em ``localhost``. O ``route`` do
Playwright não basta: ele não é chamado nos saltos de um redirecionamento nem nos
websockets, e não vê para onde um nome resolve.

Por isso o navegador abre com ``--proxy-server=socks5://127.0.0.1:<porta>`` desta
guarda. O Playwright acrescenta ``<-loopback>`` (o loopback também passa pelo proxy)
e, sem regras próprias do operador, ``MAP * ~NOTFOUND`` (o navegador não resolve nomes
sozinho). Toda conexão TCP do navegador (navegação, redirecionamentos, iframes,
workers, service workers, websockets, downloads, o TCP do WebRTC, e o proxy do
Playwright que apresenta o certificado A1, que sai pelo proxy do navegador) chega aqui
com o nome do destino; a guarda:

1. com uma lista de hosts permitidos (a execução: os ``allowed_domains`` do manifesto),
   recusa (resposta SOCKS 0x02, "proibida pela regra") todo nome fora dela e todo
   endereço literal; a sessão de gravação não tem lista;
2. resolve o nome ela mesma e recusa se qualquer endereço for da própria máquina
   (loopback, ``localhost``), link-local (o metadata da nuvem), ``0.0.0.0/8``, não
   especificado, multicast, os endereços de metadata fora do link-local
   (``fd00:ec2::/32`` da AWS, ``100.100.100.200``) ou a forma IPv6 de um deles; com
   uma :class:`EgressPolicy` (a execução e a sessão em pods, ou fora do modo de
   desenvolvimento), recusa também todo endereço que não é da internet (as redes
   privadas, a ``100.64.0.0/10`` e as reservadas), menos as redes internas que o
   operador permitiu (``RAPA_REDES_INTERNAS_PERMITIDAS``);
3. conecta exatamente ao endereço que conferiu (uma troca de DNS no meio não muda o
   destino; um endereço literal vai na forma canônica, e um com zona, ``%...``, é
   recusado), ou, com o proxy de saída do operador (``RAPA_PROXY_SAIDA``, um proxy HTTP
   CONNECT), manda o nome para ele, que resolve (só nomes e endereços conferidos: nada
   se injeta na linha do ``CONNECT``): quando a guarda não resolve o nome
   (a internet só sai pelo proxy) ou ele resolve para a internet. Um nome que resolve
   para uma rede interna permitida vai direto, sem o proxy; um que resolve para outra
   rede interna é recusado mesmo com o proxy;
4. só aceita o método sem autenticação e o comando CONNECT (BIND e UDP ASSOCIATE
   recebem 0x07), com prazo para o aperto de mão, para a resolução e para a conexão,
   e no máximo :data:`MAX_CONNECTIONS` conexões ao mesmo tempo.

O UDP não passa pelo proxy, então fica fora do navegador: quem abre o navegador
desliga o QUIC (``--disable-quic``; com proxy SOCKS o Chromium já não o usa), força o
WebRTC a só usar o que passa pelo proxy
(``--webrtc-ip-handling-policy=disable_non_proxied_udp``, que o Chromium completo
respeita e o ``chrome-headless-shell`` ignora) e, para os dois, tira o WebRTC das
páginas (:data:`WEBRTC_OFF_SCRIPT`, em todo documento, também em iframes e janelas
novas). Nenhum ``--disable-features`` é acrescentado: o Chromium só obedece ao
último, e ele desfaria a lista do próprio Playwright. Os interruptores de proxy do
operador (:data:`PROXY_SWITCHES`) saem dos argumentos do navegador
(:func:`without_proxy_switches`): qualquer um deles tiraria a guarda do caminho.

Os nomes que o operador mapeia explicitamente em ``--host-resolver-rules`` (``MAP
nome endereço``; os portais de teste em ``*.rapa.test``) valem como mapeados, mesmo
para o loopback: é configuração do operador, nunca da pessoa (com uma lista de hosts,
o nome ainda precisa estar nela).

**Resíduos conhecidos:** sem :class:`EgressPolicy` (a sessão local no modo de
desenvolvimento), as redes privadas (10/8, 172.16/12, 192.168/16, fc00::/7)
continuam abertas, porque os portais de intranet são o caso de uso. As sub-redes do
banco, da API do Kubernetes e dos outros serviços do plano de controle precisam ser
fechadas também pela NetworkPolicy do deploy, e uma rede interna permitida que as
inclua as abre. Com o proxy de saída, um nome que a guarda não consegue resolver vai
para o proxy, e o que ele alcança é a regra dele.
"""

import asyncio
import contextlib
import fnmatch
import ipaddress
import json
import logging
import re
import socket
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

__all__ = [
    "BROWSER_NETWORK_ARGS",
    "MAX_CONNECTIONS",
    "PROXY_SWITCHES",
    "WEBRTC_OFF_SCRIPT",
    "EgressGuard",
    "EgressPolicy",
    "HostRule",
    "Route",
    "UpstreamProxy",
    "blocked_address",
    "internal_host",
    "parse_host_rules",
    "parse_networks",
    "without_proxy_switches",
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
#: Prazo do outro sentido depois que um lado da conexão fechou.
HALF_CLOSE_S = 60.0
_CHUNK = 65536

#: Argumentos do Chromium que tiram o UDP do caminho (o proxy só leva TCP). Vêm depois
#: dos argumentos do operador, para não serem desfeitos por eles.
BROWSER_NETWORK_ARGS = (
    "--disable-quic",
    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
)

_WEBRTC_NAMES = (
    "RTCPeerConnection",
    "webkitRTCPeerConnection",
    "RTCDataChannel",
    "RTCIceCandidate",
    "RTCIceTransport",
    "RTCDtlsTransport",
    "RTCSctpTransport",
    "RTCRtpSender",
    "RTCRtpReceiver",
    "RTCRtpTransceiver",
    "RTCSessionDescription",
    "RTCCertificate",
)
#: Script de toda página da sessão: sem WebRTC, nenhum UDP sai do navegador, nem no
#: ``chrome-headless-shell``, que ignora a política do WebRTC.
WEBRTC_OFF_SCRIPT = (
    "(() => { for (const name of "
    + json.dumps(list(_WEBRTC_NAMES))
    + ") { try { delete globalThis[name]; } catch (e) {} } })();"
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
type Network = ipaddress.IPv4Network | ipaddress.IPv6Network

#: Os argumentos do operador que decidem por onde o navegador sai: o Chromium obedece ao
#: último ``--proxy-server`` e ao último ``--proxy-bypass-list`` (que desfaz o
#: ``<-loopback>``), e ``--no-proxy-server`` vence os dois; qualquer um deles tiraria a
#: guarda do caminho. Valem para nada no navegador guardado (:func:`without_proxy_switches`).
PROXY_SWITCHES = frozenset(
    {"proxy-server", "no-proxy-server", "proxy-pac-url", "proxy-auto-detect", "proxy-bypass-list"}
)

#: Redes que uma rede interna permitida nunca abre: a própria máquina, o link-local (o
#: metadata da nuvem), o não especificado, o multicast, o broadcast, os endereços de
#: metadata fora do link-local e a forma IPv6 mapeada de qualquer IPv4.
_NEVER_OPEN: tuple[Network, ...] = tuple(
    ipaddress.ip_network(net)
    for net in (
        "0.0.0.0/8",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "224.0.0.0/4",
        "255.255.255.255/32",
        "100.100.100.200/32",
        "::/128",
        "::1/128",
        "::ffff:0:0/96",
        "fe80::/10",
        "ff00::/8",
        "fd00:ec2::/32",
    )
)
#: Um nome que pode ir ao proxy de saída (nunca um espaço, um CR ou um LF no pedido).
_NAME = re.compile(r"^[a-z0-9_](?:[a-z0-9_.-]{0,252})$")


def without_proxy_switches(args: Iterable[str]) -> list[str]:
    """Os argumentos do operador sem os que mudam o proxy do navegador."""
    return [a for a in args if a.split("=", 1)[0].lstrip("-") not in PROXY_SWITCHES]


def parse_networks(raw: str | Iterable[str]) -> tuple[Network, ...]:
    """As redes internas permitidas (``"10.20.0.0/16, 192.168.5.0/24"`` ou uma lista).

    Cada rede precisa ser exata (``10.20.0.0/16``, nunca ``10.20.1.0/16``, que abriria
    mais do que parece) e não pode incluir nada de :data:`_NEVER_OPEN`. A mensagem de
    erro cita só a rede recusada.
    """
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    networks: list[Network] = []
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        try:
            network = ipaddress.ip_network(text, strict=True)
        except ValueError:
            raise ValueError(
                f"rede interna inválida: '{text}' (use o formato exato, como 10.20.0.0/16)"
            ) from None
        if any(_overlap(network, never) for never in _NEVER_OPEN):
            raise ValueError(
                f"a rede interna '{text}' inclui endereços que a guarda sempre recusa "
                "(a própria máquina, o link-local, o metadata da nuvem ou o multicast)"
            )
        networks.append(network)
    return tuple(dict.fromkeys(networks))


def _overlap(a: Network, b: Network) -> bool:
    if isinstance(a, ipaddress.IPv4Network) and isinstance(b, ipaddress.IPv4Network):
        return a.overlaps(b)
    if isinstance(a, ipaddress.IPv6Network) and isinstance(b, ipaddress.IPv6Network):
        return a.overlaps(b)
    return False


@dataclass(frozen=True, slots=True)
class UpstreamProxy:
    """O proxy HTTP CONNECT de saída do operador (``RAPA_PROXY_SAIDA``)."""

    host: str
    port: int

    @classmethod
    def parse(cls, url: str) -> UpstreamProxy:
        """``http://proxy.interno:3128``: só ``http``, com a porta, sem usuário nem caminho."""
        try:
            split = urlsplit(url.strip())
            port = split.port
        except ValueError:
            raise ValueError("o proxy de saída precisa ser um endereço http://host:porta") from None
        if split.scheme != "http" or not split.hostname:
            raise ValueError("o proxy de saída precisa ser um endereço http://host:porta")
        if split.username is not None or split.password is not None:
            # Uma senha aqui iria para a linha de comando do executor e para os logs.
            raise ValueError("o proxy de saída não pode ter usuário e senha no endereço")
        if port is None:
            raise ValueError("informe a porta do proxy de saída (ex. http://proxy.interno:3128)")
        if split.path not in ("", "/") or split.query or split.fragment:
            raise ValueError("o proxy de saída é só http://host:porta, sem caminho")
        return cls(split.hostname, port)

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"


@dataclass(frozen=True, slots=True)
class EgressPolicy:
    """A rede interna fechada, menos as redes permitidas, e o proxy de saída.

    Sem política (a sessão local no modo de desenvolvimento), só a própria máquina, o
    link-local e o metadata ficam fechados (:func:`blocked_address`); com ela, todo
    endereço que não é da internet também fica, menos :attr:`allowed_networks`.
    """

    #: Redes internas que os robôs (e a sessão) podem alcançar: os portais internos.
    allowed_networks: tuple[Network, ...] = ()
    #: Proxy HTTP CONNECT por onde sai a internet, quando ela só sai por ele.
    upstream_proxy: UpstreamProxy | None = None

    @classmethod
    def parse(cls, networks: str | Iterable[str] = (), proxy: str | None = None) -> EgressPolicy:
        return cls(
            allowed_networks=parse_networks(networks),
            upstream_proxy=UpstreamProxy.parse(proxy) if proxy else None,
        )

    def reachable(self, address: str) -> bool:
        """O endereço (já fora de :func:`blocked_address`) é da internet ou de uma rede permitida?"""
        ip = _address(address)
        if ip is None:
            return False
        return ip.is_global or self.internal_allowed(address)

    def internal_allowed(self, address: str) -> bool:
        """O endereço está numa rede interna permitida?"""
        ip = _address(address)
        if ip is None:
            return False
        return any(
            ip in network for network in self.allowed_networks if network.version == ip.version
        )

    def worker_args(self) -> list[str]:
        """Os argumentos do worker que levam esta política (``--guarda-de-saida`` à parte)."""
        args = [f"--rede-interna-permitida={network}" for network in self.allowed_networks]
        if self.upstream_proxy is not None:
            args.append(f"--proxy-saida={self.upstream_proxy.url}")
        return args


def _address(address: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """O endereço, com o IPv4 que um IPv6 carrega no lugar dele (como :func:`blocked_address`)."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = _embedded_ipv4(ip)
        if embedded is not None:
            return embedded
    return ip


def _literal(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """O endereço literal exato (sem zona), ou ``None`` se o texto não é um endereço."""
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class Route:
    """Para onde a guarda leva uma conexão: os endereços conferidos, ou o proxy de saída."""

    #: Os destinos (endereço e porta); pelo proxy, o nome (ou endereço) pedido e a porta.
    targets: tuple[tuple[str, int], ...]
    #: A conexão vai pelo proxy de saída (``CONNECT`` com o nome; o proxy resolve).
    via_proxy: bool = False


def blocked_address(address: str) -> bool:
    """O endereço é da própria máquina, link-local, de metadata ou sem destino válido?"""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return True
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = _embedded_ipv4(ip)
        if embedded is not None:
            # O IPv4 dentro do IPv6 (mapeado, compatível, NAT64 ou 6to4) vale como ele.
            ip = embedded
    if isinstance(ip, ipaddress.IPv4Address) and ip in _THIS_NETWORK:
        return True
    if any(ip in network for network in _METADATA_NETWORKS):
        return True
    if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast:
        return True
    return _assigned_here(ip)


_IPV4_COMPATIBLE = ipaddress.ip_network("::/96")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """O IPv4 que um IPv6 carrega: mapeado (::ffff:a.b.c.d), compatível (::a.b.c.d), NAT64
    (64:ff9b::a.b.c.d) ou 6to4 (2002:aabb:ccdd::). ``::`` e ``::1`` ficam como IPv6."""
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip in _NAT64 or (ip in _IPV4_COMPATIBLE and int(ip) > 1):
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def _assigned_here(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """O endereço é de uma interface desta máquina (o IP do pod ou da máquina da API)?

    Um serviço que escuta em ``0.0.0.0`` (a API num contêiner) responde também no
    endereço da própria máquina, fora do loopback. Só um endereço local aceita
    ``bind``; com ``ip_nonlocal_bind`` ligado, todos aceitam e a guarda recusa tudo
    (falha fechada).
    """
    family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
    try:
        sock = socket.socket(family, socket.SOCK_STREAM)
    except OSError:
        return False
    with sock:
        try:
            sock.bind((str(ip), 0))
        except OSError:
            return False
    return True


def _normalize(host: str) -> str:
    return host.strip("[]").rstrip(".").lower()


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
    """Proxy SOCKS5 local do navegador guardado (ver o módulo)."""

    def __init__(
        self,
        rules: Sequence[HostRule] = (),
        *,
        resolver: Resolver | None = None,
        on_refused: Callable[[str], None] | None = None,
        max_connections: int = MAX_CONNECTIONS,
        allowed_hosts: Iterable[str] | None = None,
        policy: EgressPolicy | None = None,
    ) -> None:
        self.rules = tuple(rules)
        self._resolve_name = resolver or _system_resolver
        self.on_refused = on_refused
        self.max_connections = max_connections
        #: Os únicos nomes que passam (a execução); ``None`` na sessão de gravação.
        self.allowed_hosts = (
            None if allowed_hosts is None else frozenset(_normalize(h) for h in allowed_hosts)
        )
        self.policy = policy
        self._server: asyncio.Server | None = None
        self._clients: set[asyncio.Task[None]] = set()
        #: Contagens (para diagnóstico e testes; nunca os destinos).
        self.allowed = 0
        self.refused = 0
        #: Conexões levadas pelo proxy de saída.
        self.proxied = 0

    @classmethod
    def from_browser_args(
        cls,
        browser_args: Sequence[str],
        *,
        on_refused: Callable[[str], None] | None = None,
        allowed_hosts: Iterable[str] | None = None,
        policy: EgressPolicy | None = None,
    ) -> EgressGuard:
        """A guarda com as regras MAP do operador nos argumentos do navegador."""
        return cls(
            parse_host_rules(browser_args),
            on_refused=on_refused,
            allowed_hosts=allowed_hosts,
            policy=policy,
        )

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
        tasks = list(self._clients)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        server.abort_clients()
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

    def _passes(self, address: str) -> bool:
        """O endereço pode ser destino (fora do que a guarda sempre recusa e da política)?"""
        if blocked_address(address):
            return False
        return self.policy is None or self.policy.reachable(address)

    def _proxy(self) -> UpstreamProxy | None:
        return self.policy.upstream_proxy if self.policy is not None else None

    async def route(self, host: str, port: int) -> Route | None:
        """Para onde levar ``host:port``; ``None`` se a guarda recusa."""
        name = _normalize(host)
        if not name:
            return None
        if self.allowed_hosts is not None and name not in self.allowed_hosts:
            return None
        rule = self._rule_for(name)
        if rule is not None and rule.target is not None:
            if rule.target.upper() == "~NOTFOUND":
                return None
            return Route((_split_target(rule.target, port),))  # configuração do operador
        if _is_localhost(name):
            return None
        if "%" in name:
            # Um endereço com zona (fe80::1%eth0) nunca é destino, e o que vem depois do
            # "%" não pode ir para a linha do CONNECT nem para a conexão.
            return None
        proxy = self._proxy()
        literal = _literal(name)
        if literal is not None:
            # Daqui em diante, só a forma canônica do endereço (nada do texto pedido).
            canonical = str(literal)
            if not self._passes(canonical):
                return None
            if proxy is not None and not self._direct_internal(canonical):
                return Route(((canonical, port),), via_proxy=True)
            return Route(((canonical, port),))
        if not _NAME.fullmatch(name):
            return None
        try:
            addresses = await asyncio.wait_for(self._resolve_name(name), RESOLVE_TIMEOUT_S)
        except OSError, TimeoutError, UnicodeError:
            addresses = []
        if not addresses:
            # Sem resposta aqui: com o proxy de saída, ele resolve (a internet só sai por ele).
            return Route(((name, port),), via_proxy=True) if proxy is not None else None
        # Um nome com qualquer resposta interna é recusado inteiro (sem escolher a "boa").
        if any(not self._passes(a) for a in addresses):
            return None
        unique = list(dict.fromkeys(addresses))
        if proxy is not None and not all(self._direct_internal(a) for a in unique):
            # A internet sai pelo proxy: o nome vai para ele, que resolve.
            return Route(((name, port),), via_proxy=True)
        return Route(tuple((address, port) for address in unique))

    def _direct_internal(self, address: str) -> bool:
        """O endereço é de uma rede interna permitida (vai direto, nunca pelo proxy)?"""
        return self.policy is not None and self.policy.internal_allowed(address)

    async def resolve(self, host: str, port: int) -> list[tuple[str, int]]:
        """Os destinos conferidos para ``host:port``; vazio se a guarda recusa.

        Pelo proxy de saída, o destino é o próprio nome pedido (ver :meth:`route`).
        """
        route = await self.route(host, port)
        return [] if route is None else list(route.targets)

    # -- protocolo -----------------------------------------------------------------

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is None or len(self._clients) >= self.max_connections or self._server is None:
            writer.transport.abort()
            return
        self._clients.add(task)
        upstream: asyncio.StreamWriter | None = None
        try:
            request = await asyncio.wait_for(self._handshake(reader, writer), HANDSHAKE_TIMEOUT_S)
            if request is None:
                return
            host, port = request
            route = await self.route(host, port)
            if route is None:
                self.refused += 1
                await _reply(writer, _NOT_ALLOWED)
                if self.on_refused is not None:
                    with contextlib.suppress(Exception):
                        self.on_refused(host)
                return
            proxy = self._proxy()
            if route.via_proxy and proxy is not None:
                opened = await _connect_via_proxy(proxy, *route.targets[0])
            else:
                opened = await self._connect(list(route.targets))
            if isinstance(opened, int):
                await _reply(writer, opened)
                return
            up_reader, upstream = opened
            self.allowed += 1
            if route.via_proxy:
                self.proxied += 1
            await _reply(writer, 0x00)
            await _splice(reader, writer, up_reader, upstream)
        except OSError, TimeoutError, asyncio.IncompleteReadError, ValueError:
            return
        finally:
            self._clients.discard(task)
            # No fim da sessão (a guarda fechando), derruba; senão, fecha depois de mandar o resto.
            closing = self._server is None
            for stream in (upstream, writer):
                if stream is not None:
                    with contextlib.suppress(Exception):
                        if closing:
                            stream.transport.abort()
                        else:
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


#: Tamanho máximo da resposta do proxy de saída ao ``CONNECT`` (só os cabeçalhos).
_PROXY_HEAD_LIMIT = 16 * 1024
#: O que pode ir como destino na linha do ``CONNECT``: um nome ou um endereço, nada mais.
_AUTHORITY_HOST = re.compile(r"^[A-Za-z0-9_.:-]{1,253}$")
_STATUS_LINE = re.compile(rb"^HTTP/1\.[01] (\d{3})[ \r]")


async def _connect_via_proxy(
    proxy: UpstreamProxy, host: str, port: int
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | int:
    """Abre o túnel ``CONNECT host:port`` no proxy de saída; o código SOCKS se falhar.

    O proxy é configuração do operador (não passa pela conferência). O nome vai
    inteiro para ele, que resolve; só um ``200`` abre o túnel. Um destino com algo além
    de letras, números, ``.``, ``-``, ``_`` e ``:`` nunca vai (nada se injeta na linha do
    ``CONNECT`` nem no ``Host``), mesmo que alguém chame isto sem passar por :meth:`EgressGuard.route`.
    """
    if not _AUTHORITY_HOST.fullmatch(host) or not 0 < port < 65536:
        return _NOT_ALLOWED
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(proxy.host, proxy.port, limit=_PROXY_HEAD_LIMIT),
            CONNECT_TIMEOUT_S,
        )
    except ConnectionRefusedError:
        return _HOST_UNREACHABLE
    except OSError, TimeoutError:
        return _HOST_UNREACHABLE
    authority = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    try:
        writer.write(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode("ascii"))
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), CONNECT_TIMEOUT_S)
    except (
        OSError,
        TimeoutError,
        UnicodeError,
        asyncio.IncompleteReadError,
        asyncio.LimitOverrunError,
    ):
        writer.transport.abort()
        return _HOST_UNREACHABLE
    match = _STATUS_LINE.match(head)
    status = int(match.group(1)) if match else 0
    if status == 200:
        return reader, writer
    writer.transport.abort()
    # O proxy recusou pela regra dele (403, ou 407 sem autenticação): proibido.
    return _NOT_ALLOWED if status in (403, 407) else _HOST_UNREACHABLE


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
            # Depois que um lado fechou, o outro tem um prazo: um destino que nunca responde
            # nem fecha não segura a vaga da guarda para sempre.
            grace = None if len(pending) == len(tasks) else HALF_CLOSE_S
            done, pending = await asyncio.wait(
                pending, timeout=grace, return_when=asyncio.FIRST_COMPLETED
            )
            if not done or any(
                task.cancelled() or task.exception() or not task.result() for task in done
            ):
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
