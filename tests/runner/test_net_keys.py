"""Chaves da rede contra ações repetidas: a página de um endereço e as voltas das repetições."""

import pytest

from rapa.runner.interpreter import _other_iteration, _page_key


@pytest.mark.parametrize(
    "url",
    [
        "https://portal.exemplo.com.br/notas",
        "https://portal.exemplo.com.br/notas/",
        "https://portal.exemplo.com.br/Notas",
        "https://PORTAL.exemplo.com.br:443/notas?pagina=2#topo",
        "http://portal.exemplo.com.br/notas",
        "https://portal.exemplo.com.br//notas//",
        "https://portal.exemplo.com.br/notas;jsessionid=ABC123",
        "https://portal.exemplo.com.br/%6Eotas",
    ],
)
def test_addresses_of_the_same_page_have_the_same_key(url: str) -> None:
    # Na dúvida, a mesma página: a mesma linha.
    assert _page_key(url) == _page_key("https://portal.exemplo.com.br/notas")


@pytest.mark.parametrize(
    "url",
    [
        "https://portal.exemplo.com.br/notas/cancelar",
        "https://portal.exemplo.com.br/filial/a/notas",
        "https://portal.exemplo.com.br:8443/notas",
        "https://outro.exemplo.com.br/notas",
    ],
)
def test_other_pages_have_other_keys(url: str) -> None:
    assert _page_key(url) != _page_key("https://portal.exemplo.com.br/notas")


def test_the_root_and_pages_without_a_host() -> None:
    assert _page_key("https://portal.exemplo.com.br") == _page_key("https://portal.exemplo.com.br/")
    assert _page_key("about:blank") != _page_key("https://portal.exemplo.com.br/")


@pytest.mark.parametrize(
    ("earlier", "current", "expected"),
    [
        ((), (), None),  # fora de repetições
        ((("cada", 0),), (("cada", 0),), None),  # a mesma volta, outro passo
        ((("cada", 0),), (("cada", 1),), ("cada", 0)),  # outra volta
        ((("um", 0),), (("outro", 0),), None),  # repetições seguidas, uma fora da outra
        ((("um", 1),), (("outro", 0),), None),
        ((("mes", 0), ("nota", 1)), (("mes", 1), ("nota", 1)), ("mes", 0)),
        ((("mes", 0), ("nota", 0)), (("mes", 0), ("nota", 1)), ("nota", 0)),
        ((("mes", 0),), (("mes", 0), ("nota", 1)), None),  # antes da de dentro, no mesmo mês
        ((("mes", 0),), (("mes", 1), ("nota", 0)), ("mes", 0)),
        ((("cada", 0), ("a", 3)), (("cada", 0), ("b", 0)), None),
    ],
)
def test_acts_conflict_only_in_other_iterations_of_a_shared_loop(
    earlier: tuple[tuple[str, int], ...],
    current: tuple[tuple[str, int], ...],
    expected: tuple[str, int] | None,
) -> None:
    assert _other_iteration(earlier, current) == expected
