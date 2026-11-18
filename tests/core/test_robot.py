import pytest
from pydantic import ValidationError

from rapa.core.robot import Effect, FieldType, Locator, Manifest
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
