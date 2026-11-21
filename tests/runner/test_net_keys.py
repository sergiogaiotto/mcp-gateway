"""Chaves da rede contra ações repetidas: a página de um endereço e as voltas das repetições."""

import time
import tracemalloc
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from rapa.core.ids import new_id
from rapa.core.robot import MAX_LIST_ITEMS, ClickStep, ForEachStep, Manifest
from rapa.runner.errors import ErrorCode, StepError
from rapa.runner.interpreter import Interpreter, _other_iteration, _page_key
from rapa.runner.ports import (
    DictSecretProvider,
    DirectoryArtifactSink,
    ListEventSink,
    LocalFileProvider,
)


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


# -- custo da rede em repetições longas ------------------------------------------------------


class _Frame:
    parent_frame = None

    def __init__(self, url: str) -> None:
        self.url = url


class _Request:
    """Um pedido de navegação da página principal, como o Playwright entrega."""

    def __init__(self, url: str, redirected_from: _Request | None = None) -> None:
        self.url = url
        self.redirected_from = redirected_from
        self.frame = _Frame(url)

    def is_navigation_request(self) -> bool:
        return True


class _FakePage:
    def __init__(self) -> None:
        self.url = f"{_BASE}/notas"


_BASE = "https://portal.exemplo.com.br"
_CANCELAR = ClickStep.model_validate(
    {
        "id": "cancelar",
        "title": "Cancelar a nota",
        "effect": "irreversible",
        "target": {
            "intent": "botão Cancelar da nota",
            "locators": [{"by": "role", "value": "button", "name": "Cancelar"}],
            "within": {"by": "role", "value": "row"},
            "within_has_text": "{{ item.numero }}",
        },
    }
)
_CADA = ForEachStep.model_validate(
    {
        "id": "cada",
        "title": "Para cada nota",
        "items": "inputs.notas",
        "max_items": MAX_LIST_ITEMS,
        "steps": [{"kind": "navigate", "id": "abrir", "title": "Abrir", "url": f"{_BASE}/"}],
    }
)

type _Body = Callable[[Interpreter, _FakePage, int], None]


async def _run_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, count: int, body: _Body
) -> None:
    """Roda a repetição ``cada`` (o contêiner de verdade) com ``count`` itens; no lugar
    dos passos de dentro, ``body`` faz o que eles fariam na rede (ações e navegações)."""
    manifest = Manifest.model_validate(
        {
            "name": "robo_de_teste",
            "title": "Robô de teste",
            "description": "Robô montado pelos testes da rede contra ações repetidas.",
            "allowed_domains": ["portal.exemplo.com.br"],
            "steps": [_CADA.model_dump(mode="json")],
        }
    )
    interpreter = Interpreter(
        manifest,
        {},
        secrets=DictSecretProvider({}),
        files=LocalFileProvider(None),
        artifacts=DirectoryArtifactSink(tmp_path),
        events=ListEventSink(),
        run_id=new_id(),
        attempt=1,
    )
    page = _FakePage()
    interpreter._page = cast(Any, page)
    interpreter._inputs = {"notas": [{"numero": f"N{index}"} for index in range(count)]}

    async def run_block(_steps: object) -> None:
        body(interpreter, page, interpreter._items[-1][1])

    monkeypatch.setattr(interpreter, "_run_block", run_block)
    await interpreter._run_container(_CADA)


def _act(interpreter: Interpreter) -> None:
    """A ação na linha do item, na página de agora."""
    act = interpreter._row_not_acted_on(_CANCELAR)
    assert act is not None
    interpreter._record_act(act)


def _navigated(interpreter: Interpreter, page: _FakePage, url: str, *, robot: bool = False) -> None:
    """A página principal vai para ``url``: pelo ``navigate`` do robô, ou não."""
    if robot:
        interpreter._robot_navigation = _page_key(url)
    try:
        interpreter._on_request(cast(Any, _Request(url)))
        page.url = url
        interpreter._on_frame_navigated(cast(Any, _Frame(url)))
    finally:
        interpreter._robot_navigation = None
        interpreter._robot_requests.clear()


def _refused(interpreter: Interpreter, page: _FakePage, numero: str, url: str) -> bool:
    """Se a ação na linha ``numero``, em ``url``, é recusada nesta volta."""
    item, index, loop = interpreter._items[-1]
    interpreter._items[-1] = ({"numero": numero}, index, loop)
    where, page.url = page.url, url
    try:
        interpreter._row_not_acted_on(_CANCELAR)
    except StepError as err:
        assert err.code == ErrorCode.ELEMENTO_AMBIGUO
        return True
    finally:
        interpreter._items[-1] = (item, index, loop)
        page.url = where
    return False


async def test_the_net_bookkeeping_is_linear_in_long_loops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Uma repetição com o maior número de itens; em cada volta, uma ação, um POST que
    # redireciona para outro endereço e o ``navigate`` do robô de volta à lista. Antes,
    # cada navegação era anotada em cada volta que já tinha acabado (minutos e gigabytes).
    count = MAX_LIST_ITEMS
    decisions: list[bool] = []

    def body(interpreter: Interpreter, page: _FakePage, index: int) -> None:
        if index < count - 1:
            _act(interpreter)
            post = _Request(f"{_BASE}/notas/cancelar")
            interpreter._on_request(cast(Any, post))
            landing = f"{_BASE}/notas/feita/{index}"
            interpreter._on_request(cast(Any, _Request(landing, post)))
            page.url = landing
            interpreter._on_frame_navigated(cast(Any, _Frame(landing)))
            _navigated(interpreter, page, f"{_BASE}/notas", robot=True)
            return
        # A última volta não age: confere as decisões. A linha da volta 0 é recusada na
        # página onde agiu e nas que a página principal abriu depois, até a última; a da
        # penúltima volta, não na página que a volta 0 abriu antes dela; outra linha, não.
        last = f"N{count - 2}"
        decisions.extend(
            [
                _refused(interpreter, page, "N0", f"{_BASE}/notas"),
                _refused(interpreter, page, "N0", f"{_BASE}/notas/feita/0"),
                _refused(interpreter, page, "N0", f"{_BASE}/notas/feita/{count - 2}"),
                _refused(interpreter, page, last, f"{_BASE}/notas/feita/{count - 2}"),
                not _refused(interpreter, page, last, f"{_BASE}/notas/feita/0"),
                not _refused(interpreter, page, "N0", f"{_BASE}/notas/nunca"),
                not _refused(interpreter, page, "outra", f"{_BASE}/notas"),
            ]
        )

    started = time.perf_counter()
    await _run_loop(tmp_path / "tempo", monkeypatch, count, body)
    elapsed = time.perf_counter() - started
    assert decisions == [True] * 7
    assert elapsed < 10, elapsed
    decisions.clear()
    tracemalloc.start()
    try:
        await _run_loop(tmp_path / "memoria", monkeypatch, count, body)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert decisions == [True] * 7
    assert peak < 64 * 2**20, peak


async def test_ended_iterations_count_every_navigation_but_the_robot_navigate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Depois do fim da volta 0, a página passa por /tarde (um temporizador ou um popup da
    # ação) e o robô abre /filial/b com um ``navigate``: só /tarde é página da ação. Uma
    # navegação de antes da ação da volta 1 nunca conta para ela.
    decisions: list[bool] = []

    def body(interpreter: Interpreter, page: _FakePage, index: int) -> None:
        if index == 0:
            _act(interpreter)
        elif index == 1:
            _navigated(interpreter, page, f"{_BASE}/tarde")
            _navigated(interpreter, page, f"{_BASE}/filial/b", robot=True)
            decisions.append(_refused(interpreter, page, "N0", f"{_BASE}/tarde"))
            decisions.append(not _refused(interpreter, page, "N0", f"{_BASE}/filial/b"))
            _navigated(interpreter, page, f"{_BASE}/antes")
            _navigated(interpreter, page, f"{_BASE}/notas", robot=True)
            _act(interpreter)
        else:
            decisions.append(not _refused(interpreter, page, "N1", f"{_BASE}/antes"))
            decisions.append(_refused(interpreter, page, "N1", f"{_BASE}/notas"))

    await _run_loop(tmp_path, monkeypatch, 3, body)
    assert decisions == [True] * 4
