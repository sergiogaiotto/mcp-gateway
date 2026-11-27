"""As políticas do OpenBao como o OpenBao as aplica, para o OpenBao falso dos testes.

Lê o HCL das políticas (o subconjunto que o RaPA usa: ``path`` com ``capabilities`` e
``allowed_parameters``) e decide cada pedido como ``vault/acl.go`` do OpenBao:

- o caminho exato vale primeiro; depois o glob (``*`` no fim) mais longo e os caminhos
  com ``+`` (um segmento inteiro, de qualquer coisa), pela prioridade do OpenBao: o
  primeiro ``+`` ou ``*`` mais tarde, sem ``*`` no fim, menos ``+``, mais longo;
- a capacidade vem da operação, e um POST vira ``create`` ou ``update`` pela conferência
  de existência do caminho (quem decide é o OpenBao falso, como cada backend);
- ``allowed_parameters``: cada campo do pedido precisa estar na lista, com um dos valores
  dela (texto com texto, pelo glob do OpenBao; o resto, igual e do mesmo tipo).
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

POLICY_FOLDER = Path(__file__).resolve().parents[3] / "deploy" / "openbao" / "politicas"
CAPABILITIES = frozenset({"create", "read", "update", "delete", "list", "sudo", "deny"})
_TOKEN = re.compile(r'\s+|#[^\n]*|"(?:[^"\\]|\\.)*"|[A-Za-z_][A-Za-z0-9_-]*|[{}\[\]=,]|.')


@dataclass
class Rule:
    capabilities: set[str] = field(default_factory=set)
    #: Campo -> valores aceitos (lista vazia: qualquer valor); ``None``: qualquer campo.
    allowed_parameters: dict[str, list[Any]] | None = None


class _Parser:
    def __init__(self, text: str) -> None:
        self.tokens = [
            tok for tok in _TOKEN.findall(text) if tok.strip() and not tok.startswith("#")
        ]
        self.pos = 0

    def peek(self) -> str | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self, expected: str | None = None) -> str:
        tok = self.peek()
        if tok is None or (expected is not None and tok != expected):
            raise ValueError(f"política fora do formato: esperava {expected!r}, veio {tok!r}")
        self.pos += 1
        return tok

    def value(self) -> Any:
        tok = self.take()
        if tok.startswith('"'):
            return tok[1:-1]
        if tok in {"true", "false"}:
            return tok == "true"
        if tok == "[":
            items: list[Any] = []
            while self.peek() != "]":
                items.append(self.value())
                if self.peek() == ",":
                    self.take(",")
            self.take("]")
            return items
        if tok == "{":
            obj: dict[str, Any] = {}
            while self.peek() != "}":
                key = self.take().strip('"')
                self.take("=")
                obj[key] = self.value()
                if self.peek() == ",":
                    self.take(",")
            self.take("}")
            return obj
        raise ValueError(f"política fora do formato: valor {tok!r}")


def parse_policy(text: str) -> dict[str, Rule]:
    """As regras de uma política, por caminho. Recusa o que o falso não sabe aplicar."""
    parser = _Parser(text)
    rules: dict[str, Rule] = {}
    while parser.peek() is not None:
        parser.take("path")
        path = parser.take()
        if not path.startswith('"') or path[1:-1] in rules:
            raise ValueError(f"caminho fora do formato ou repetido: {path}")
        body = parser.value()
        if not isinstance(body, dict) or not set(body) <= {
            "capabilities",
            "allowed_parameters",
        }:
            raise ValueError(f"o falso não sabe aplicar {sorted(body)} em {path}")
        caps = body.get("capabilities")
        if not isinstance(caps, list) or not caps or not set(caps) <= CAPABILITIES:
            raise ValueError(f"capacidades fora do formato em {path}")
        allowed = body.get("allowed_parameters")
        if allowed is not None and not (
            isinstance(allowed, dict) and all(isinstance(v, list) for v in allowed.values())
        ):
            raise ValueError(f"allowed_parameters fora do formato em {path}")
        rules[path[1:-1]] = Rule(
            set(caps),
            None if allowed is None else {k.lower(): v for k, v in allowed.items()},
        )
    return rules


def load_policy(name: str) -> dict[str, Rule]:
    return parse_policy((POLICY_FOLDER / f"{name}.hcl").read_text(encoding="utf-8"))


def _glob_match(pattern: str, value: str) -> bool:
    # strutil.GlobbedStringsMatch do OpenBao: * só nas pontas.
    if pattern == "*":
        return True
    if len(pattern) >= 2 and pattern.startswith("*") and pattern.endswith("*"):
        return pattern[1:-1] in value
    if pattern.startswith("*"):
        return value.endswith(pattern[1:])
    if pattern.endswith("*"):
        return value.startswith(pattern[:-1])
    return pattern == value


def _value_allowed(value: Any, allowed: list[Any]) -> bool:
    if not allowed:
        return True
    for item in allowed:
        if isinstance(item, str) and isinstance(value, str):
            if _glob_match(item, value):
                return True
        elif type(item) is type(value) and item == value:
            return True
    return False


class ACL:
    """As políticas de um token, juntas (como o OpenBao junta as de um token)."""

    def __init__(self, policies: Iterable[Mapping[str, Rule]]) -> None:
        self.exact: dict[str, Rule] = {}
        self.prefix: dict[str, Rule] = {}
        self.segments: dict[str, Rule] = {}
        for policy in policies:
            for path, rule in policy.items():
                if "+*" in path:
                    raise ValueError(f"uso inválido de curingas: {path}")
                has_segments = path == "+" or "/+" in path or path.startswith("+/")
                if has_segments:
                    target, key = self.segments, path
                elif path.endswith("*"):
                    target, key = self.prefix, path[:-1]
                else:
                    target, key = self.exact, path
                merged = target.setdefault(key, Rule())
                merged.capabilities |= rule.capabilities
                if rule.allowed_parameters is not None:
                    merged.allowed_parameters = {
                        **(merged.allowed_parameters or {}),
                        **rule.allowed_parameters,
                    }

    def _non_exact(self, path: str) -> Rule | None:
        # (primeiro curinga, sem * no fim, -curingas, tamanho, texto): o maior vence.
        found: list[tuple[tuple[int, bool, int, int, str], Rule]] = []
        prefixes = [p for p in self.prefix if path.startswith(p)]
        if prefixes:
            best = max(prefixes, key=len)
            found.append(((len(best), False, 0, len(best), best), self.prefix[best]))
        parts = path.split("/")
        for full, rule in self.segments.items():
            first = full.index("+")
            current, is_prefix = full, full.endswith("*")
            if is_prefix:
                current = current[:-1]
            segments = current.split("/")
            if len(parts) < len(segments) or (not is_prefix and len(parts) != len(segments)):
                continue
            wildcards = 0
            for i, segment in enumerate(segments):
                if segment == "+":
                    wildcards += 1
                elif segment == parts[i]:
                    continue
                elif is_prefix and i == len(segments) - 1 and parts[i].startswith(segment):
                    continue
                else:
                    break
            else:
                key = (first, not is_prefix, -wildcards, len(current), current)
                found.append((key, rule))
        if not found:
            return None
        return max(found, key=lambda item: item[0])[1]

    def rule_for(self, path: str, operation: str) -> Rule | None:
        if path in self.exact:
            return self.exact[path]
        if operation == "list":
            if path.removesuffix("/") in self.exact:
                return self.exact[path.removesuffix("/")]
            if path.endswith("/") and (rule := self._non_exact(path[:-1])) is not None:
                return rule
        return self._non_exact(path)

    def allows(self, path: str, operation: str, data: Mapping[str, Any] | None = None) -> bool:
        """``operation``: read, list, create, update ou delete."""
        rule = self.rule_for(path, operation)
        if rule is None or "deny" in rule.capabilities or operation not in rule.capabilities:
            return False
        if operation not in {"read", "create", "update"} or not data:
            return True
        allowed = rule.allowed_parameters
        if not allowed or set(allowed) == {"*"}:
            return True
        for name, value in data.items():
            values = allowed.get(name.lower())
            if values is None:
                if "*" not in allowed:
                    return False
                continue
            if not _value_allowed(value, values):
                return False
        return True
