"""Robôs de exemplo para o portal de teste.

Servem aos testes do executor, do plano de controle e do gateway MCP, e como
modelo do que o compilador deve produzir.
"""

from typing import Any

from rapa.core.robot import Manifest

__all__ = ["baixar_notas", "emitir_protocolo", "login_steps", "portal_credentials"]


def _target(intent: str, *locators: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"intent": intent, "locators": list(locators), **extra}


def portal_credentials(base_url: str) -> list[dict[str, Any]]:
    origin = base_url
    return [
        {"name": "portal", "kind": "password", "origin": origin, "description": "Acesso ao portal"},
        {"name": "portal_otp", "kind": "totp", "origin": origin, "description": "Segundo fator"},
    ]


def login_steps(base_url: str) -> list[dict[str, Any]]:
    return [
        {
            "kind": "navigate",
            "id": "abrir_login",
            "title": "Abrir a página de login",
            "url": f"{base_url}/login",
        },
        {
            "kind": "fill",
            "id": "digitar_usuario",
            "title": "Digitar o usuário",
            "target": _target(
                "campo Usuário",
                {"by": "label", "value": "Usuário"},
                {"by": "css", "value": "input[name=usuario]"},
            ),
            "value": {"source": "secret", "credential": "portal", "field": "username"},
        },
        {
            "kind": "fill",
            "id": "digitar_senha",
            "title": "Digitar a senha",
            "target": _target("campo Senha", {"by": "label", "value": "Senha"}),
            "value": {"source": "secret", "credential": "portal", "field": "password"},
        },
        {
            "kind": "click",
            "id": "entrar",
            "title": "Clicar em Entrar",
            "target": _target("botão Entrar", {"by": "role", "value": "button", "name": "Entrar"}),
            "expect_navigation": True,
        },
        {
            "kind": "fill",
            "id": "digitar_codigo",
            "title": "Digitar o código de verificação",
            "target": _target(
                "campo Código de verificação", {"by": "label", "value": "Código de verificação"}
            ),
            "value": {"source": "secret", "credential": "portal_otp", "field": "totp"},
        },
        {
            "kind": "click",
            "id": "confirmar_codigo",
            "title": "Confirmar o código",
            "target": _target(
                "botão Confirmar", {"by": "role", "value": "button", "name": "Confirmar"}
            ),
            "expect_navigation": True,
        },
        {
            "kind": "wait_for",
            "id": "aguardar_inicio",
            "title": "Aguardar a página inicial",
            "target": _target(
                "título Início", {"by": "role", "value": "heading", "name": "Início"}
            ),
        },
    ]


def emitir_protocolo(base_url: str, host: str) -> Manifest:
    """Login com TOTP e emissão de um protocolo (passo irreversível)."""
    return Manifest.model_validate(
        {
            "name": "emitir_protocolo",
            "title": "Emitir protocolo",
            "description": "Entra no portal de teste e emite um protocolo para uma pessoa.",
            "allowed_domains": [host],
            "credentials": portal_credentials(base_url),
            "inputs": [
                {"name": "nome", "title": "Nome completo", "type": "string"},
                {"name": "cpf", "title": "CPF", "type": "cpf"},
                {"name": "uf", "title": "UF", "type": "string", "example": "SP"},
            ],
            "steps": [
                *login_steps(base_url),
                {
                    "kind": "navigate",
                    "id": "abrir_protocolo",
                    "title": "Abrir o formulário",
                    "url": f"{base_url}/protocolo",
                },
                {
                    "kind": "fill",
                    "id": "digitar_nome",
                    "title": "Digitar o nome",
                    "target": _target(
                        "campo Nome completo", {"by": "label", "value": "Nome completo"}
                    ),
                    "value": {"source": "text", "template": "{{ inputs.nome }}"},
                },
                {
                    "kind": "fill",
                    "id": "digitar_cpf",
                    "title": "Digitar o CPF",
                    "target": _target(
                        "campo CPF",
                        {"by": "label", "value": "CPF"},
                        {"by": "placeholder", "value": "000.000.000-00"},
                    ),
                    "value": {"source": "text", "template": "{{ inputs.cpf }}"},
                    "sequential": True,
                },
                {
                    "kind": "select",
                    "id": "escolher_uf",
                    "title": "Escolher a UF",
                    "target": _target("lista UF", {"by": "label", "value": "UF"}),
                    "option": "{{ inputs.uf }}",
                },
                {
                    "kind": "check",
                    "id": "marcar_declaracao",
                    "title": "Marcar a declaração",
                    "target": _target(
                        "caixa da declaração",
                        {"by": "label", "value": "Declaro que as informações são verdadeiras"},
                    ),
                },
                {
                    "kind": "click",
                    "id": "emitir",
                    "title": "Clicar em Emitir",
                    "effect": "irreversible",
                    "target": _target(
                        "botão Emitir", {"by": "role", "value": "button", "name": "Emitir"}
                    ),
                    "expect_navigation": True,
                },
                {
                    "kind": "extract_text",
                    "id": "ler_protocolo",
                    "title": "Ler o número do protocolo",
                    "target": _target(
                        "número do protocolo", {"by": "test_id", "value": "protocolo"}
                    ),
                    "into": "protocolo",
                },
            ],
            "outputs": [
                {
                    "name": "protocolo",
                    "title": "Número do protocolo",
                    "type": "string",
                    "source": "vars.protocolo",
                }
            ],
            "postconditions": [{"op": "text_contains", "text": "Protocolo emitido"}],
        }
    )


def baixar_notas(base_url: str, host: str) -> Manifest:
    """Login, leitura da tabela de notas e download do arquivo de cada uma (só leitura)."""
    return Manifest.model_validate(
        {
            "name": "baixar_notas",
            "title": "Baixar notas fiscais",
            "description": "Lê a primeira página de notas e baixa o arquivo de cada uma.",
            "allowed_domains": [host],
            "credentials": portal_credentials(base_url),
            "steps": [
                *login_steps(base_url),
                {
                    "kind": "navigate",
                    "id": "abrir_notas",
                    "title": "Abrir as notas fiscais",
                    "url": f"{base_url}/notas",
                },
                {
                    "kind": "extract_table",
                    "id": "ler_notas",
                    "title": "Ler a tabela de notas",
                    "target": _target(
                        "tabela de notas",
                        {"by": "role", "value": "table", "name": "Notas fiscais emitidas"},
                    ),
                    "into": "notas",
                    "columns": ["numero", "cliente", "valor", "situacao", "arquivo"],
                },
                {
                    "kind": "for_each",
                    "id": "cada_nota",
                    "title": "Para cada nota",
                    "items": "vars.notas",
                    "steps": [
                        {
                            "kind": "download",
                            "id": "baixar_nota",
                            "title": "Baixar o arquivo da nota",
                            "target": _target(
                                "link Baixar da nota",
                                {"by": "role", "value": "link", "name": "Baixar"},
                                within={"by": "role", "value": "row"},
                                within_has_text="{{ item.numero }}",
                            ),
                            "save_as": "nota-{{ item.numero }}.pdf",
                            "into": "arquivo_nota",
                        }
                    ],
                },
            ],
            "outputs": [
                {"name": "notas", "title": "Notas lidas", "type": "list", "source": "vars.notas"}
            ],
        }
    )
