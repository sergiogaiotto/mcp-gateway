"""O OpenBao falso aplica as políticas como o OpenBao (``vault/acl.go`` e
``vault/request_handling.go``): se ele errar a regra, os testes das políticas não valem."""

import pytest

from .acl import ACL, parse_policy
from .fakebao import FakeBao, _Key


def _acl(text: str) -> ACL:
    return ACL([parse_policy(text)])


def test_a_glob_that_starts_its_wildcard_later_wins_over_a_segment_wildcard() -> None:
    acl = _acl(
        'path "t/keys/empresa-*" { capabilities = ["read"] }\n'
        'path "t/keys/+/config" { capabilities = ["update"] }'
    )
    # O glob tem o primeiro curinga mais tarde: vence em empresa-x/config também.
    assert not acl.allows("t/keys/empresa-x/config", "update")
    assert acl.allows("t/keys/empresa-x/config", "read")
    assert acl.allows("t/keys/outra/config", "update")


def test_plus_is_a_whole_segment_and_exact_paths_win() -> None:
    acl = _acl(
        'path "t/keys/+" { capabilities = ["read"] }\n'
        'path "t/keys/empresa-+" { capabilities = ["delete"] }\n'
        'path "t/keys/a" { capabilities = ["list"] }'
    )
    assert acl.allows("t/keys/qualquer", "read")
    assert not acl.allows("t/keys/qualquer/config", "read")  # um segmento só
    assert not acl.allows("t/keys/empresa-x", "delete")  # "empresa-+" é um nome literal
    assert acl.allows("t/keys/empresa-+", "delete")
    assert acl.allows("t/keys/a", "list") and not acl.allows("t/keys/a", "read")


def test_the_longest_glob_wins() -> None:
    acl = _acl(
        'path "kv/*" { capabilities = ["read", "delete"] }\n'
        'path "kv/data/*" { capabilities = ["read"] }'
    )
    assert acl.allows("kv/data/x", "read") and not acl.allows("kv/data/x", "delete")
    assert acl.allows("kv/metadata/x", "delete")


def test_allowed_parameters_compare_text_with_text() -> None:
    acl = _acl(
        'path "t/keys/+/config" {\n'
        '  capabilities = ["update"]\n'
        '  allowed_parameters = { "deletion_allowed" = ["true"] }\n'
        "}"
    )
    path = "t/keys/k/config"
    assert acl.allows(path, "update", {"deletion_allowed": "true"})
    assert acl.allows(path, "update", {})  # sem campos, vale (como no OpenBao)
    assert not acl.allows(path, "update", {"deletion_allowed": True})  # bool não é texto
    assert not acl.allows(path, "update", {"deletion_allowed": "false"})
    assert not acl.allows(path, "update", {"deletion_allowed": "true", "exportable": True})
    assert not acl.allows(path, "create", {"deletion_allowed": "true"})


def test_the_parser_refuses_what_the_fake_cannot_apply() -> None:
    for text in (
        'path "a" { capabilities = ["read"] denied_parameters = { "x" = [] } }',
        'path "a" { capabilities = ["ler"] }',
        'path "a" { capabilities = ["read"] }\npath "a" { capabilities = ["read"] }',
        'path "a+*" { capabilities = ["read"] }',
    ):
        with pytest.raises(ValueError):
            ACL([parse_policy(text)])


def test_a_post_is_create_or_update_by_the_existence_check_of_each_path() -> None:
    bao = FakeBao()
    bao.keys["empresa-x"] = _Key(b"\0" * 32)
    # O caminho das chaves não confere se a chave existe: um POST é sempre "update".
    assert bao._operation("POST", "rapa-transit/keys/empresa-nova") == "update"
    assert bao._operation("POST", "rapa-transit/keys/empresa-x/config") == "update"
    # O encrypt confere: "create" só enquanto a chave não existe.
    assert bao._operation("POST", "rapa-transit/encrypt/empresa-nova") == "create"
    assert bao._operation("POST", "rapa-transit/encrypt/empresa-x") == "update"
    assert bao._operation("POST", "rapa-transit/decrypt/empresa-nova") == "update"
    bao.put_secret("empresas/e/portal", {"password": "x"})
    assert bao._operation("POST", "rapa/data/empresas/e/portal") == "update"
    assert bao._operation("POST", "rapa/data/empresas/e/outro") == "create"
