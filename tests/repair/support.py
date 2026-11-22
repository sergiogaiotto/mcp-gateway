"""Retratos de página escritos à mão, como o executor os entrega (sem navegador)."""

from typing import Any

from rapa.core.robot import Manifest
from rapa.core.snapshot import PageSnapshot, SnapshotFrame
from rapa.fixtures.robots import baixar_notas, emitir_protocolo

BASE = "http://portal-v2.rapa.test:8080"
HOST = "portal-v2.rapa.test"


def element(**fields: Any) -> dict[str, Any]:
    counts = fields.pop("counts", {})
    return {"counts": counts, **fields}


def snapshot(step_id: str, kind: str, *elements: dict[str, Any], **extra: Any) -> PageSnapshot:
    return PageSnapshot.model_validate(
        {
            "step_id": step_id,
            "step_kind": kind,
            "error_code": extra.pop("error_code", "elemento_nao_encontrado"),
            "url": f"{BASE}/login",
            "title": "Entrar · Portal de Teste",
            "frames": extra.pop("frames", [SnapshotFrame().model_dump()]),
            "elements": list(elements),
            **extra,
        }
    )


def login_page(step_id: str = "entrar", kind: str = "click") -> PageSnapshot:
    """A página de login do portal v2: o botão agora se chama "Acessar"."""
    return snapshot(
        step_id,
        kind,
        element(tag="a", role="link", name="Início", text="Início", counts={"role_name": 1}),
        element(tag="a", role="link", name="Sair", text="Sair", counts={"role_name": 1}),
        element(
            tag="input",
            input_type="text",
            role="textbox",
            name="Login",
            label="Login",
            name_attr="usuario",
            id="login",
            autocomplete="username",
            counts={"role_name": 1, "label": 1, "name_attr": 1, "id": 1},
        ),
        element(
            tag="input",
            input_type="password",
            role="textbox",
            name="Senha",
            label="Senha",
            name_attr="senha",
            id="senha",
            autocomplete="current-password",
            is_password=True,
            sensitive=True,
            counts={"role_name": 1, "label": 1, "name_attr": 1, "id": 1},
        ),
        element(
            tag="button",
            role="button",
            name="Acessar",
            text="Acessar",
            counts={"role_name": 1, "text": 1},
        ),
    )


def protocolo() -> Manifest:
    return emitir_protocolo(BASE, HOST)


def notas() -> Manifest:
    return baixar_notas(BASE, HOST)
