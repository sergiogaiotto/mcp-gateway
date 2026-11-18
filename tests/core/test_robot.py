import pytest
from pydantic import ValidationError

from rapa.core.robot import CredentialRef, Effect, FieldType, Locator, Manifest
from rapa.fixtures.robots import baixar_notas, emitir_protocolo

BASE = "http://portal-v1.rapa.test:8080"
HOST = "portal-v1.rapa.test"


def test_sample_robots_are_valid() -> None:
    protocolo = emitir_protocolo(BASE, HOST)
    assert protocolo.effect() is Effect.IRREVERSIBLE
    notas = baixar_notas(BASE, HOST)
    assert notas.effect() is Effect.READ
    assert [s.id for s, parents in notas.iter_steps() if parents] == ["baixar_nota"]


def test_input_schema_for_mcp_and_rest() -> None:
    schema = emitir_protocolo(BASE, HOST).input_json_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["required"] == ["nome", "cpf", "uf"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["cpf"]["pattern"].startswith("^[0-9]{3}")


def _lista(name: str) -> dict[str, object]:
    return {
        "name": name,
        "title": name,
        "type": "list",
        "item_fields": [{"name": "n", "title": "N", "type": "string"}],
    }


def _cada(step_id: str, items: str, max_items: int | None = None) -> dict[str, object]:
    step: dict[str, object] = {
        "kind": "for_each",
        "id": step_id,
        "title": "Para cada item",
        "items": items,
        "steps": [{"kind": "navigate", "id": f"{step_id}_abrir", "title": "Abrir", "url": BASE}],
    }
    if max_items is not None:
        step["max_items"] = max_items
    return step


def test_list_input_schema_takes_the_loop_limit() -> None:
    manifest = Manifest.model_validate(
        {
            "name": "listas",
            "title": "Listas",
            "description": "Robô com várias listas.",
            "allowed_domains": [HOST],
            "inputs": [_lista("a"), _lista("b"), _lista("c"), _lista("d")],
            "steps": [
                _cada("cada_a", "inputs.a", 5),
                {
                    "kind": "if",
                    "id": "se",
                    "title": "Se",
                    "condition": {"op": "url_contains", "value": "x"},
                    "then": [_cada("cada_a_de_novo", "inputs.a", 3)],
                    "otherwise": [_cada("cada_b", "inputs.b")],
                },
                _cada("cada_tabela", "vars.c", 2),
            ],
        }
    )
    properties = manifest.input_json_schema()["properties"]
    assert properties["a"]["maxItems"] == 3  # o menor limite entre as repetições
    assert properties["b"]["maxItems"] == 1000  # o limite padrão de for_each
    assert properties["c"]["maxItems"] == 10_000  # só uma variável com o mesmo nome
    assert properties["d"]["maxItems"] == 10_000  # nenhuma repetição percorre a lista


def test_fingerprint_is_stable() -> None:
    a = emitir_protocolo(BASE, HOST)
    b = Manifest.model_validate(a.model_dump(mode="json"))
    assert a.fingerprint() == b.fingerprint()


def test_role_locator_requires_known_role() -> None:
    with pytest.raises(ValidationError, match="papel ARIA desconhecido"):
        Locator.model_validate({"by": "role", "value": "botao", "name": "Emitir"})


def test_name_only_for_role() -> None:
    with pytest.raises(ValidationError, match="só se aplica"):
        Locator.model_validate({"by": "css", "value": "button", "name": "Emitir"})


@pytest.mark.parametrize("domain", ["10.0.0.1", "localhost", "*.gov.br", "exa mple.com"])
def test_rejects_bad_domains(domain: str) -> None:
    data = emitir_protocolo(BASE, HOST).model_dump(mode="json")
    data["allowed_domains"] = [domain]
    with pytest.raises(ValidationError):
        Manifest.model_validate(data)


def test_rejects_unknown_fields() -> None:
    data = emitir_protocolo(BASE, HOST).model_dump(mode="json")
    data["steps"][0]["javascript"] = "alert(1)"
    with pytest.raises(ValidationError):
        Manifest.model_validate(data)


def test_list_input_needs_item_fields() -> None:
    data = emitir_protocolo(BASE, HOST).model_dump(mode="json")
    data["inputs"].append({"name": "linhas", "title": "Linhas", "type": FieldType.LIST.value})
    with pytest.raises(ValidationError, match="item_fields"):
        Manifest.model_validate(data)


def test_credential_origin_requires_tls_outside_test_domain() -> None:
    data = emitir_protocolo(BASE, HOST).model_dump(mode="json")
    data["credentials"][0]["origin"] = "http://portal.exemplo.com.br"
    with pytest.raises(ValidationError, match="https"):
        Manifest.model_validate(data)


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("https://Portal.Exemplo.com.br", "https://portal.exemplo.com.br"),
        ("https://portal.exemplo.com.br:443/", "https://portal.exemplo.com.br"),
        ("https://portal.exemplo.com.br:8443", "https://portal.exemplo.com.br:8443"),
        ("http://portal-v1.rapa.test:80", "http://portal-v1.rapa.test"),
        ("http://portal-v1.rapa.test:8080", "http://portal-v1.rapa.test:8080"),
    ],
)
def test_credential_origin_is_normalized_like_the_browser(given: str, expected: str) -> None:
    ref = CredentialRef(name="portal", kind="password", origin=given)
    assert ref.origin == expected


def test_credential_origin_with_bad_port_is_refused() -> None:
    with pytest.raises(ValidationError, match="porta"):
        CredentialRef(name="portal", kind="password", origin="https://portal.exemplo.com.br:99999")


def test_runner_job_deadline_needs_a_timezone() -> None:
    from datetime import UTC, datetime

    from rapa.core.contracts import RunnerJob
    from rapa.core.ids import new_id

    base = {
        "run_id": new_id(),
        "company_id": new_id(),
        "attempt": 1,
        "manifest": emitir_protocolo(BASE, HOST),
        "inputs": {},
    }
    RunnerJob.model_validate({**base, "deadline": datetime.now(UTC)})
    with pytest.raises(ValidationError, match="timezone"):
        RunnerJob.model_validate({**base, "deadline": datetime(2026, 11, 18, 12, 0)})
