"""Descrição em pt-BR e diferença entre versões, sobre os robôs de exemplo."""

from typing import Any

from rapa.core.robot import Effect, Manifest
from rapa.fixtures.portal import DEFAULT_PASSWORD, DEFAULT_TOTP_SECRET, DEFAULT_USER
from rapa.fixtures.robots import baixar_notas, emitir_protocolo
from rapa.ui import describe

BASE = "http://portal-v1.rapa.test:8080"
HOST = "portal-v1.rapa.test"


def _protocolo() -> Manifest:
    return emitir_protocolo(BASE, HOST)


def _notas() -> Manifest:
    return baixar_notas(BASE, HOST)


def _edit(manifest: Manifest, change: Any) -> Manifest:
    data = manifest.model_dump(mode="json")
    change(data)
    return Manifest.model_validate(data)


def _step(data: dict[str, Any], step_id: str) -> dict[str, Any]:
    return next(s for s in data["steps"] if s["id"] == step_id)


def test_numbered_steps_in_portuguese() -> None:
    lines = describe.describe_steps(_protocolo())
    rendered = [line.render(badge=False) for line in lines]
    assert rendered[0] == "1. Abrir a página de login (portal-v1.rapa.test/login)"
    assert "10. Digitar o CPF em “campo CPF” (valor da entrada CPF)" in rendered
    assert "11. Escolher a UF em “lista UF” (opção da entrada UF)" in rendered
    assert "12. Marcar a declaração (marcar “caixa da declaração”)" in rendered
    emitir = next(line for line in lines if line.step_id == "emitir")
    assert emitir.effect is Effect.IRREVERSIBLE
    assert emitir.badge == "Irreversível"
    assert emitir.render().endswith("[Irreversível]")
    assert {line.badge for line in lines} == {"Leitura", "Irreversível"}


def test_secrets_only_as_vault_references() -> None:
    text = describe.render_text(_protocolo()) + describe.render_text(_notas())
    assert "valor do cofre: portal, usuário" in text
    assert "valor do cofre: portal, senha" in text
    assert "valor do cofre: portal_otp, código de verificação" in text
    for secret in (DEFAULT_PASSWORD, DEFAULT_TOTP_SECRET, DEFAULT_USER):
        assert secret not in text
    credentials = describe.describe_credentials(_protocolo())
    assert [(c.name, c.kind) for c in credentials] == [
        ("portal", "Usuário e senha"),
        ("portal_otp", "Código de verificação (TOTP)"),
    ]


def test_nested_blocks_are_indented() -> None:
    lines = describe.describe_steps(_notas())
    loop = next(line for line in lines if line.step_id == "cada_nota")
    child = next(line for line in lines if line.step_id == "baixar_nota")
    assert loop.number == "10" and loop.depth == 0
    assert child.number == "10.1" and child.depth == 1
    assert child.render(badge=False) == (
        "    10.1. Baixar o arquivo da nota de “link Baixar da nota” "
        "(salva como “nota-[campo numero do item].pdf”)"
    )
    assert "repete para cada item da tabela notas" in loop.text


def test_if_with_otherwise_block() -> None:
    def add_if(data: dict[str, Any]) -> None:
        data["steps"].append(
            {
                "kind": "if",
                "id": "se_aviso",
                "title": "Se aparecer um aviso",
                "condition": {"op": "text_contains", "text": "Aviso"},
                "then": [
                    {
                        "kind": "click",
                        "id": "fechar_aviso",
                        "title": "Fechar o aviso",
                        "target": {
                            "intent": "botão Fechar",
                            "locators": [{"by": "role", "value": "button", "name": "Fechar"}],
                        },
                    }
                ],
                "otherwise": [
                    {
                        "kind": "press",
                        "id": "apertar_esc",
                        "title": "Apertar Esc",
                        "key": "Escape",
                    }
                ],
            }
        )

    lines = describe.describe_steps(_edit(_protocolo(), add_if))
    tail = [line.render(badge=False) for line in lines[-4:]]
    assert tail == [
        "15. Se aparecer um aviso (se a página contém “Aviso”)",
        "    15.1. Fechar o aviso (clicar em “botão Fechar”)",
        "    Caso contrário:",
        "    15.2. Apertar Esc (tecla Escape)",
    ]


def test_inputs_and_outputs_tables() -> None:
    inputs = describe.describe_inputs(_protocolo())
    assert [(i.name, i.title, i.type, i.required) for i in inputs] == [
        ("nome", "Nome completo", "Texto", "Obrigatória"),
        ("cpf", "CPF", "CPF", "Obrigatória"),
        ("uf", "UF", "Texto", "Obrigatória"),
    ]
    assert inputs[2].example == "SP"
    outputs = describe.describe_outputs(_protocolo())
    assert [(o.name, o.type, o.source) for o in outputs] == [("protocolo", "Texto", "protocolo")]
    assert describe.describe_postconditions(_protocolo()) == ["a página contém “Protocolo emitido”"]
    notas = describe.describe_outputs(_notas())
    assert notas[0].type == "Lista"


def test_literal_and_mixed_values() -> None:
    def literal(data: dict[str, Any]) -> None:
        _step(data, "digitar_nome")["value"] = {"source": "text", "template": "Fulano"}
        _step(data, "digitar_cpf")["value"] = {
            "source": "text",
            "template": "CPF {{ inputs.cpf }} de {{ inputs.nome }}",
        }

    lines = {
        line.step_id: line.text for line in describe.describe_steps(_edit(_protocolo(), literal))
    }
    assert lines["digitar_nome"].endswith("(texto fixo “Fulano”)")
    assert lines["digitar_cpf"].endswith("(texto “CPF [entrada CPF] de [entrada Nome completo]”)")


def test_diff_added_removed_changed() -> None:
    old = _protocolo()

    def change(data: dict[str, Any]) -> None:
        _step(data, "digitar_nome")["title"] = "Digitar o nome completo"
        _step(data, "digitar_cpf")["value"] = {
            "source": "secret",
            "credential": "portal",
            "field": "username",
        }
        _step(data, "ler_protocolo")["effect"] = "write"
        data["steps"] = [s for s in data["steps"] if s["id"] != "marcar_declaracao"]
        data["steps"].append(
            {
                "kind": "wait_for",
                "id": "aguardar_recibo",
                "title": "Aguardar o recibo",
                "url_contains": "/recibo",
            }
        )
        data["inputs"][2]["title"] = "Estado"

    new = _edit(old, change)
    diff = describe.diff_manifests(old, new)
    assert [c.step_id for c in diff.added] == ["aguardar_recibo"]
    assert [c.step_id for c in diff.removed] == ["marcar_declaracao"]
    changed = {c.step_id: c for c in diff.changed}
    assert set(changed) == {"digitar_nome", "digitar_cpf", "ler_protocolo"}
    (title,) = changed["digitar_nome"].fields
    assert (title.label, title.before, title.after) == (
        "título",
        "Digitar o nome",
        "Digitar o nome completo",
    )
    (value,) = changed["digitar_cpf"].fields
    assert value.before == "valor da entrada CPF"
    assert value.after == "valor do cofre: portal, usuário"
    (effect,) = changed["ler_protocolo"].fields
    assert (effect.before, effect.after) == ("Leitura", "Grava dados")
    assert diff.raises_effect
    assert [(i.label, i.before, i.after) for i in diff.info] == [
        ("entrada uf", "UF: Texto, obrigatória", "Estado: Texto, obrigatória")
    ]
    assert not diff.empty


def test_diff_target_and_reorder() -> None:
    old = _protocolo()

    def change(data: dict[str, Any]) -> None:
        cpf = _step(data, "digitar_cpf")
        cpf["target"]["intent"] = "campo do CPF"
        cpf["target"]["locators"] = [{"by": "test_id", "value": "cpf"}]
        steps = data["steps"]
        index = next(i for i, s in enumerate(steps) if s["id"] == "escolher_uf")
        steps[index], steps[index + 1] = steps[index + 1], steps[index]

    diff = describe.diff_manifests(old, _edit(old, change))
    changed = {c.step_id: c for c in diff.changed}
    cpf = {f.field: f for f in changed["digitar_cpf"].fields}
    assert cpf["target.intent"].after == "“campo do CPF”"
    assert cpf["target.locators"].before == "rótulo “CPF”; texto de exemplo “000.000.000-00”"
    assert cpf["target.locators"].after == "identificador de teste “cpf”"
    moved = [c for c in diff.changed if any(f.field == "position" for f in c.fields)]
    assert len(moved) == 1 and moved[0].step_id in {"escolher_uf", "marcar_declaracao"}
    assert not diff.added and not diff.removed
    assert not diff.raises_effect


def test_quoted_intents_are_not_quoted_again() -> None:
    """O compilador e o conserto já põem o rótulo entre aspas (``botão “Acessar”``)."""
    old = _protocolo()

    def change(data: dict[str, Any]) -> None:
        _step(data, "emitir")["target"]["intent"] = "botão “Acessar”"

    new = _edit(old, change)
    diff = describe.diff_manifests(old, new)
    fields = {f.field: f for c in diff.changed for f in c.fields}
    assert (fields["target.intent"].before, fields["target.intent"].after) == (
        "“botão Emitir”",
        "botão “Acessar”",
    )
    [line] = [line for line in describe.describe_steps(new) if line.step_id == "emitir"]
    assert "(clicar em botão “Acessar” e esperar a nova página)" in line.render(badge=False)


def test_diff_moving_into_a_block() -> None:
    old = _notas()

    def change(data: dict[str, Any]) -> None:
        ler = next(s for s in data["steps"] if s["id"] == "abrir_notas")
        data["steps"].remove(ler)
        loop = _step(data, "cada_nota")
        loop["steps"].insert(0, ler)

    diff = describe.diff_manifests(old, _edit(old, change))
    (moved,) = diff.changed
    assert moved.step_id == "abrir_notas"
    (position,) = moved.fields
    assert (position.before, position.after) == (
        "nível principal",
        "dentro de cada_nota (repetição)",
    )


def test_identical_versions_have_empty_diff() -> None:
    diff = describe.diff_manifests(_protocolo(), _protocolo())
    assert diff.empty
    assert not diff.raises_effect
