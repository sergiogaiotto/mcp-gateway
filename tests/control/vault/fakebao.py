"""Um OpenBao falso para os testes (aplicação ASGI): entrada kubernetes, tokens com prazo e
políticas, KV v2 (dados e metadados), transit (chaves, embrulhar e desembrulhar) e a saúde.

Imita só o que o RaPA usa, com as respostas do OpenBao de verdade (os códigos e o formato
do JSON). Os tokens vencem pelo relógio dado (o mesmo do cliente, nos testes).

Cada papel entra com as políticas de ``deploy/openbao/politicas`` (as mesmas que o
``preparar.sh`` escreve), e cada pedido passa pelas políticas do token como no OpenBao
(:mod:`.acl`). Um POST vira ``create`` ou ``update`` pela conferência de existência de
cada caminho, como nos backends do OpenBao: ``transit/keys/<nome>`` (e o que vem depois
dele) não tem conferência, então é sempre ``update``; ``transit/encrypt/<nome>`` tem, e
é ``create`` enquanto a chave não existe, quando o próprio encrypt cria a chave (com os
padrões: nunca exportável, sem cópia em claro); no KV, ``create`` se o segredo não
existe.
"""

import base64
import os
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .acl import ACL, POLICY_FOLDER, Rule, load_policy

ROLE = "rapa-despachante"
WEB_ROLE = "rapa-web"
API_ROLE = "rapa-api"
OPERATION_ROLE = "rapa-operacao"
SA_TOKEN = "jwt-de-teste-da-conta-de-servico"


def script_roles() -> dict[str, tuple[str, ...]]:
    """Papel -> políticas, lidos do próprio ``preparar.sh`` (``role <papel> "$CONTA" <política>``)."""
    text = (POLICY_FOLDER.parent / "preparar.sh").read_text(encoding="utf-8")
    found = re.findall(r'^role (\S+) "\$\w+" (\S+)$', text, flags=re.MULTILINE)
    return {name: (policy,) for name, policy in found}


#: Papel -> políticas, como o preparar.sh escreve (o da interface e o da API com a mesma).
ROLES = script_roles()


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@dataclass
class _Key:
    material: bytes
    exportable: bool = False
    plaintext_backup: bool = False
    deletion_allowed: bool = False
    version: int = 1


@dataclass
class _Token:
    expires: float
    policies: tuple[str, ...]


def _weak_bool(value: Any) -> bool:
    # mapstructure.WeakDecode do OpenBao (strconv.ParseBool para texto).
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value != 0
    if isinstance(value, str):
        if value in {"1", "t", "T", "TRUE", "true", "True"}:
            return True
        if value in {"", "0", "f", "F", "FALSE", "false", "False"}:
            return False
    raise ValueError("valor booleano inválido")


@dataclass
class FakeBao:
    clock: Callable[[], float] = field(default_factory=FakeClock)
    jwt: str = SA_TOKEN
    lease_s: int = 60
    kv: str = "rapa"
    transit: str = "rapa-transit"
    auth: str = "kubernetes"
    #: Papel -> políticas do token que a entrada com ele recebe.
    roles: dict[str, tuple[str, ...]] = field(default_factory=lambda: dict(ROLES))
    #: Política -> regras; por padrão, as de ``deploy/openbao/politicas``.
    policies: dict[str, dict[str, Rule]] = field(
        default_factory=lambda: {
            name: load_policy(name) for policies in ROLES.values() for name in policies
        }
    )
    #: Caminho no KV (sem a montagem) -> dados da versão atual.
    secrets: dict[str, dict[str, Any]] = field(default_factory=dict)
    keys: dict[str, _Key] = field(default_factory=dict)
    tokens: dict[str, _Token] = field(default_factory=dict)
    #: Prefixos (sem a montagem do KV) que a política nega sempre.
    denied: set[str] = field(default_factory=set)
    #: Caminhos que respondem com um redirecionamento.
    redirects: set[str] = field(default_factory=set)
    sealed: bool = False
    renew_fails: bool = False
    logins: int = 0
    renewals: int = 0
    #: (método, caminho) de cada pedido.
    requests: list[tuple[str, str]] = field(default_factory=list)
    #: Os corpos recebidos (para conferir o que o cliente manda).
    bodies: list[Any] = field(default_factory=list)
    #: (operação, caminho, papel ou políticas) de cada pedido que as políticas recusaram.
    refusals: list[tuple[str, str, tuple[str, ...]]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.app = Starlette(
            routes=[
                Route(
                    "/v1/{path:path}",
                    self.handle,
                    methods=["GET", "POST", "PUT", "DELETE", "LIST"],
                )
            ]
        )

    # -- ajudantes dos testes ------------------------------------------------------------

    def put_secret(self, path: str, data: dict[str, Any]) -> None:
        self.secrets[path] = dict(data)

    def expire_all_tokens(self) -> None:
        self.tokens.clear()

    def issue_token(
        self, policies: tuple[str, ...] = (ROLE,), *, expires: float = float("inf")
    ) -> str:
        """Um token com estas políticas (o de desenvolvimento, sem entrada)."""
        token = "hvs." + secrets.token_urlsafe(16)
        self.tokens[token] = _Token(expires, policies)
        return token

    # -- servidor -------------------------------------------------------------------------

    def _token(self, request: Request) -> _Token | None:
        token = self.tokens.get(request.headers.get("x-vault-token") or "")
        if token is None or self.clock() >= token.expires:
            return None
        return token

    def _issue(self, policies: tuple[str, ...]) -> dict[str, Any]:
        token = self.issue_token(policies, expires=self.clock() + self.lease_s)
        return {
            "client_token": token,
            "lease_duration": self.lease_s,
            "renewable": True,
            "policies": list(policies),
        }

    @staticmethod
    def _denied() -> JSONResponse:
        return JSONResponse({"errors": ["permission denied"]}, status_code=403)

    def _operation(self, method: str, path: str) -> str:
        """A operação do pedido, com a conferência de existência de cada backend."""
        if method == "LIST":
            return "list"
        if method == "GET":
            return "read"
        if method == "DELETE":
            return "delete"
        transit = f"{self.transit}/encrypt/"
        if path.startswith(transit):
            return "update" if path.removeprefix(transit) in self.keys else "create"
        for kind in ("data", "metadata"):
            kv = f"{self.kv}/{kind}/"
            if path.startswith(kv):
                return "update" if path.removeprefix(kv) in self.secrets else "create"
        return "update"  # sem conferência de existência: sempre uma mudança

    def _allowed(self, token: _Token, operation: str, path: str, body: Any) -> bool:
        acl = ACL(self.policies.get(name, {}) for name in token.policies)
        data = body if isinstance(body, dict) else None
        if acl.allows(path, operation, data):
            return True
        self.refusals.append((operation, path, token.policies))
        return False

    async def handle(self, request: Request) -> Response:
        path = request.path_params["path"]
        method = request.method
        if method == "GET" and request.query_params.get("list") == "true":
            method = "LIST"
        self.requests.append((method, path))
        body: Any = None
        if method in {"POST", "PUT"}:
            body = await request.json()
            self.bodies.append({"path": path, "body": body})
        if path in self.redirects:
            return Response(status_code=307, headers={"Location": "https://outro.test/v1/" + path})
        if path == "sys/health":
            return JSONResponse(
                {"initialized": True, "sealed": self.sealed},
                status_code=503 if self.sealed else 200,
            )
        if path == f"auth/{self.auth}/login" and method in {"POST", "PUT"}:
            self.logins += 1
            role = body.get("role")
            if role not in self.roles:
                return JSONResponse({"errors": ["invalid role name"]}, status_code=400)
            if body.get("jwt") != self.jwt:
                return self._denied()
            return JSONResponse({"auth": self._issue(self.roles[role])})
        token = self._token(request)
        if token is None:
            return self._denied()
        list_path = path if method != "LIST" or path.endswith("/") else path + "/"
        if not self._allowed(token, self._operation(method, path), list_path, body):
            return self._denied()
        if path == "auth/token/renew-self":
            if self.renew_fails:
                return self._denied()
            self.renewals += 1
            token.expires = self.clock() + self.lease_s
            value = request.headers["x-vault-token"]
            return JSONResponse(
                {"auth": {"client_token": value, "lease_duration": self.lease_s, "renewable": True}}
            )
        if path.startswith(f"{self.kv}/"):
            return self._kv(method, path.removeprefix(f"{self.kv}/"))
        if path.startswith(f"{self.transit}/"):
            try:
                return self._transit(method, path.removeprefix(f"{self.transit}/"), body)
            except ValueError as exc:
                return JSONResponse({"errors": [str(exc)]}, status_code=400)
        return JSONResponse({"errors": ["no handler for route"]}, status_code=404)

    def _kv(self, method: str, rest: str) -> Response:
        kind, _, key = rest.partition("/")
        if any(key.startswith(prefix) for prefix in self.denied):
            return self._denied()
        if kind == "data" and method == "GET":
            data = self.secrets.get(key)
            if data is None:
                return JSONResponse({"errors": []}, status_code=404)
            return JSONResponse({"data": {"data": data, "metadata": {"version": 1}}})
        if kind == "metadata" and method == "LIST":
            prefix = key if key.endswith("/") else key + "/"
            children = sorted(
                {
                    path[len(prefix) :].split("/", 1)[0]
                    + ("/" if "/" in path[len(prefix) :] else "")
                    for path in self.secrets
                    if path.startswith(prefix)
                }
            )
            if not children:
                return JSONResponse({"errors": []}, status_code=404)
            return JSONResponse({"data": {"keys": children}})
        if kind == "metadata" and method == "DELETE":
            self.secrets.pop(key, None)
            return Response(status_code=204)
        return JSONResponse({"errors": ["unsupported"]}, status_code=405)

    def _key_info(self, name: str, key: _Key) -> JSONResponse:
        return JSONResponse(
            {
                "data": {
                    "name": name,
                    "type": "aes256-gcm96",
                    "exportable": key.exportable,
                    "allow_plaintext_backup": key.plaintext_backup,
                    "deletion_allowed": key.deletion_allowed,
                    "latest_version": key.version,
                }
            }
        )

    def _transit(self, method: str, rest: str, body: Any) -> Response:
        parts = rest.split("/")
        if parts[0] == "keys" and len(parts) == 2:
            name = parts[1]
            key = self.keys.get(name)
            if method == "GET":
                if key is None:
                    return JSONResponse({"errors": []}, status_code=404)
                return self._key_info(name, key)
            if method == "POST":
                if key is None:
                    self.keys[name] = _Key(
                        os.urandom(32),
                        exportable=_weak_bool(body.get("exportable", False)),
                        plaintext_backup=_weak_bool(body.get("allow_plaintext_backup", False)),
                    )
                return Response(status_code=204)
            if method == "DELETE":
                if key is None:
                    return Response(status_code=204)
                if not key.deletion_allowed:
                    return JSONResponse(
                        {"errors": ["deletion is not allowed for this key"]}, status_code=400
                    )
                del self.keys[name]
                return Response(status_code=204)
        if parts[0] == "keys" and len(parts) == 3 and method == "POST":
            key = self.keys.get(parts[1])
            if key is None:
                # Como o OpenBao: mexer numa chave que não existe é um pedido inválido
                # (400), não um 404.
                message = f"no existing key named {parts[1]} could be found"
                return JSONResponse({"errors": [message]}, status_code=400)
            if parts[2] == "config":
                if "deletion_allowed" in body:
                    key.deletion_allowed = _weak_bool(body["deletion_allowed"])
                # Os dois só ligam, nunca desligam (como no OpenBao).
                key.exportable |= _weak_bool(body.get("exportable", False))
                key.plaintext_backup |= _weak_bool(body.get("allow_plaintext_backup", False))
                return self._key_info(parts[1], key)
            if parts[2] == "rotate":
                key.version += 1
                return self._key_info(parts[1], key)
            if parts[2] == "trim":
                return self._key_info(parts[1], key)
        if parts[0] in {"encrypt", "decrypt"} and len(parts) == 2 and method == "POST":
            key = self.keys.get(parts[1])
            if key is None and parts[0] == "encrypt":
                # O próprio encrypt cria a chave que não existe (com "create" na política),
                # com os padrões do transit: nunca exportável, sem cópia em claro.
                if body.get("type", "aes256-gcm96") != "aes256-gcm96":
                    raise ValueError("unsupported key type")
                key = self.keys[parts[1]] = _Key(os.urandom(32))
            if key is None:
                return JSONResponse({"errors": ["encryption key not found"]}, status_code=400)
            aead = AESGCM(key.material)
            if parts[0] == "encrypt":
                nonce = os.urandom(12)
                sealed = aead.encrypt(nonce, base64.b64decode(body["plaintext"]), None)
                text = base64.b64encode(nonce + sealed).decode()
                return JSONResponse(
                    {
                        "data": {
                            "ciphertext": f"vault:v{key.version}:{text}",
                            "key_version": key.version,
                        }
                    }
                )
            raw = base64.b64decode(body["ciphertext"].split(":", 2)[2])
            try:
                plain = aead.decrypt(raw[:12], raw[12:], None)
            except Exception:
                return JSONResponse({"errors": ["cipher: message authentication failed"]}, 400)
            return JSONResponse({"data": {"plaintext": base64.b64encode(plain).decode()}})
        return JSONResponse({"errors": ["unsupported"]}, status_code=405)
