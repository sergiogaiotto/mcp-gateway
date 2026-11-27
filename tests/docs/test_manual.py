"""O manual de instalação, configuração e testes (``docs/manual/index.html``).

A página é lida com o ``html.parser`` da biblioteca padrão. Os testes falham se:

- uma imagem (``<img src>``) não existe ou fica sem texto alternativo;
- uma referência interna (``href="#..."``, ``aria-labelledby``, ``aria-controls`` e
  ``url(#...)`` do SVG) não tem o ``id`` correspondente, ou um link relativo aponta para um
  arquivo que não existe;
- a página carrega algo remoto: ``src``, ``srcset``, o ``href`` de qualquer elemento que não
  seja ``<a>``, ``url(...)`` e ``@import`` no CSS, ou um endereço dentro de um ``<script>``
  (o manual precisa abrir sem internet, direto do repositório); só um ``<a href>`` comum pode
  apontar para fora;
- um nome ``RAPA_*`` citado não é lido pelo código: ele precisa aparecer em ``src/``,
  ``deploy/``, no workflow do CI ou, para os dos testes (``RAPA_TEST_*``), em ``tests/`` fora
  desta pasta; ``RAPA_X_*`` vale se algum nome do código começa com ``RAPA_X_``;
- um caminho do repositório citado (``deploy/...``, ``docs/...``, ``src/...``, ``tests/...``,
  ``.github/...``, ``README.md``, ``pyproject.toml``, ``uv.lock``) não existe; um
  ``deploy/rapa-...`` é o Deployment do ``kubectl``, não um caminho;
- uma imagem de ``docs/manual/img/capturas.json`` não aparece no manual.
"""

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import cache
from html.parser import HTMLParser
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MANUAL_DIR = REPO / "docs" / "manual"
MANUAL = MANUAL_DIR / "index.html"
CAPTURES = MANUAL_DIR / "img" / "capturas.json"

#: Onde o código lê as variáveis (os testes só para as deles; esta pasta nunca conta).
CODE_ROOTS = (REPO / "src", REPO / "deploy", REPO / ".github")
TEST_ROOT = REPO / "tests"
_HERE = Path(__file__).resolve().parent

_REMOTE_URL = re.compile(r"^\s*(?:[a-z][a-z0-9+.-]*:)?//", re.IGNORECASE)
_CSS_REMOTE = re.compile(r"url\(\s*['\"]?\s*(?:[a-z][a-z0-9+.-]*:)?//|@import", re.IGNORECASE)
_SCRIPT_REMOTE = re.compile(r"(?:https?:)?//[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", re.IGNORECASE)
_URL_REF = re.compile(r"url\(\s*#([^)\s]+)\s*\)")
#: Atributos que carregam um recurso (em qualquer elemento) e os que só um ``<a>`` pode
#: apontar para fora.
_LOADING_ATTRS = frozenset(
    {"src", "srcset", "poster", "data", "action", "formaction", "background", "xlink:href"}
)
_RAPA_NAME = re.compile(r"\bRAPA_[A-Z0-9_]*[A-Z0-9](?:_\*)?")
_CODE_NAME = re.compile(r"RAPA_[A-Z0-9_]*[A-Z0-9]")
_REPO_PATH = re.compile(r"(?<![\w./-])(?:\.github|deploy|docs|src|tests)/[\w.*/-]*")
_ROOT_FILE = re.compile(r"(?<![\w./-])(?:README\.md|pyproject\.toml|uv\.lock)\b")
#: ``kubectl ... deploy/rapa-web``: o recurso do Kubernetes, não uma pasta do repositório.
_KUBECTL_REF = re.compile(r"deploy/rapa-[a-z-]+")


@dataclass
class Page:
    """O que os testes precisam da página."""

    ids: set[str] = field(default_factory=set)
    #: (elemento, atributo, valor) de cada atributo.
    attributes: list[tuple[str, str, str]] = field(default_factory=list)
    #: O texto da página, sem os ``<script>`` e os ``<style>``.
    text: list[str] = field(default_factory=list)
    scripts: list[str] = field(default_factory=list)
    styles: list[str] = field(default_factory=list)
    images: list[tuple[str, str | None]] = field(default_factory=list)


class _Reader(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.page = Page()
        self._raw: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        if "id" in values:
            self.page.ids.add(values["id"])
        for name, value in values.items():
            self.page.attributes.append((tag, name, value))
        if tag == "img":
            self.page.images.append((values.get("src", ""), dict(attrs).get("alt")))
        if tag in {"script", "style"}:
            self._raw = tag

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag in {"script", "style"}:
            self._raw = None

    def handle_endtag(self, tag: str) -> None:
        if tag == self._raw:
            self._raw = None

    def handle_data(self, data: str) -> None:
        if self._raw == "script":
            self.page.scripts.append(data)
        elif self._raw == "style":
            self.page.styles.append(data)
        else:
            self.page.text.append(data)


def read_page(html: str) -> Page:
    reader = _Reader()
    reader.feed(html)
    reader.close()
    return reader.page


@cache
def manual() -> Page:
    return read_page(MANUAL.read_text(encoding="utf-8"))


def remote_resources(page: Page) -> list[str]:
    """O que a página carregaria de fora (um ``<a href>`` para fora é só um link)."""
    found: list[str] = []
    for tag, name, value in page.attributes:
        if name in _LOADING_ATTRS or (name == "href" and tag != "a"):
            urls = [part.strip().split(" ")[0] for part in value.split(",")]
            if name != "srcset":
                urls = [value]
            found.extend(f"<{tag} {name}={url!r}>" for url in urls if _REMOTE_URL.match(url))
        if name == "style" and _CSS_REMOTE.search(value):
            found.append(f"<{tag} style={value!r}>")
    found.extend(f"<style>: {m.group(0)!r}" for s in page.styles for m in _CSS_REMOTE.finditer(s))
    found.extend(
        f"<script>: {m.group(0)!r}" for s in page.scripts for m in _SCRIPT_REMOTE.finditer(s)
    )
    return found


def _text_files(roots: Iterable[Path], skip: Path | None = None) -> Iterable[str]:
    for root in roots:
        for path in root.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if skip is not None and path.is_relative_to(skip):
                continue
            try:
                yield path.read_text(encoding="utf-8")
            except UnicodeDecodeError, OSError:
                continue


@cache
def code_names() -> frozenset[str]:
    """Os nomes ``RAPA_*`` que o código, o deploy, o CI e os testes leem."""
    names: set[str] = set()
    for text in _text_files(CODE_ROOTS):
        names.update(_CODE_NAME.findall(text))
    for text in _text_files([TEST_ROOT], skip=_HERE):
        names.update(_CODE_NAME.findall(text))
    return frozenset(names)


def unknown_names(text: str, known: frozenset[str]) -> set[str]:
    missing: set[str] = set()
    for name in _RAPA_NAME.findall(text):
        if name.endswith("_*"):
            prefix = name[:-1]
            if not any(code.startswith(prefix) for code in known):
                missing.add(name)
        elif name not in known:
            missing.add(name)
    return missing


def repository_paths(text: str) -> set[str]:
    paths = {match.rstrip(".") for match in _REPO_PATH.findall(text)}
    paths = {path for path in paths if not _KUBECTL_REF.fullmatch(path)}
    return paths | set(_ROOT_FILE.findall(text))


def _exists(path: str) -> bool:
    if "*" in path:
        return any(REPO.glob(path))
    return (REPO / path).exists()


def test_the_manual_exists_and_is_utf8() -> None:
    html = MANUAL.read_text(encoding="utf-8")
    assert html.lstrip().lower().startswith("<!doctype html>")
    assert '<html lang="pt-BR">' in html


def test_images_exist_and_have_alt_text() -> None:
    images = manual().images
    assert images, "o manual não tem imagens"
    missing = [src for src, _ in images if not (MANUAL_DIR / src).is_file()]
    assert not missing, f"imagens que não existem: {missing}"
    without_alt = [src for src, alt in images if not (alt or "").strip()]
    assert not without_alt, f"imagens sem texto alternativo: {without_alt}"


def test_internal_references_resolve() -> None:
    page = manual()
    broken: list[str] = []
    for _, name, value in page.attributes:
        if name == "href" and value.startswith("#"):
            if value[1:] not in page.ids:
                broken.append(value)
        elif name in {"aria-labelledby", "aria-controls", "aria-describedby"}:
            broken.extend(f"{name}={ref}" for ref in value.split() if ref not in page.ids)
        broken.extend(f"url(#{ref})" for ref in _URL_REF.findall(value) if ref not in page.ids)
    assert not broken, f"referências internas sem destino: {sorted(set(broken))}"


def test_relative_links_exist() -> None:
    broken: list[str] = []
    for _, name, value in manual().attributes:
        if name not in {"href", "src"} or not value or value.startswith(("#", "mailto:")):
            continue
        if _REMOTE_URL.match(value) or re.match(r"^[a-z][a-z0-9+.-]*:", value, re.IGNORECASE):
            continue
        target = value.split("#", 1)[0].split("?", 1)[0]
        if target and not (MANUAL_DIR / target).exists():
            broken.append(value)
    assert not broken, f"links relativos sem arquivo: {broken}"


def test_nothing_remote_is_loaded() -> None:
    found = remote_resources(manual())
    assert not found, f"o manual carrega recursos de fora: {found}"


def test_the_remote_check_catches_what_it_should() -> None:
    page = read_page(
        """<!doctype html><html><head>
        <link rel="stylesheet" href="https://fonts.example.test/fonte.css">
        <script src="//cdn.example.test/lib.js"></script>
        <style>@import "x.css"; body { background: url(https://img.example.test/a.png); }</style>
        <script>fetch("https://api.example.test/dados");</script>
        </head><body>
        <a href="https://docs.example.test/">documentação</a>
        <img src="img/local.png" srcset="img/a.png 1x, https://img.example.test/b.png 2x" alt="x">
        </body></html>"""
    )
    found = remote_resources(page)
    assert any("fonts.example.test" in item for item in found)
    assert any("cdn.example.test" in item for item in found)
    assert any("@import" in item for item in found)
    assert any("img.example.test/a.png" in item or "url(" in item for item in found)
    assert any("api.example.test" in item for item in found)
    assert any("img.example.test/b.png" in item for item in found)
    assert not any("docs.example.test" in item for item in found)
    assert not any("img/local.png" in item for item in found)


def test_rapa_names_are_read_by_the_code() -> None:
    text = "\n".join(manual().text)
    assert _RAPA_NAME.search(text), "o manual não cita nenhuma variável"
    missing = unknown_names(text, code_names())
    assert not missing, f"variáveis citadas que o código não lê: {sorted(missing)}"


def test_the_name_check_catches_an_invented_variable() -> None:
    known = frozenset({"RAPA_BAO_ENDERECO", "RAPA_DEV"})
    assert unknown_names("RAPA_DEV e RAPA_BAO_*", known) == set()
    assert unknown_names("RAPA_INVENTADA e RAPA_S3_*", known) == {"RAPA_INVENTADA", "RAPA_S3_*"}


def test_repository_paths_exist() -> None:
    paths = repository_paths("\n".join(manual().text))
    assert "deploy/openbao/preparar.sh" in paths
    missing = sorted(path for path in paths if not _exists(path))
    assert not missing, f"caminhos do repositório que não existem: {missing}"


def test_the_path_check_reads_paths_and_skips_kubectl_resources() -> None:
    found = repository_paths(
        "rode deploy/kind/instalar.sh. Veja kubectl logs deploy/rapa-despachante, "
        "/v1/docs e README.md; o chart em deploy/helm/rapa/."
    )
    assert found == {"deploy/kind/instalar.sh", "README.md", "deploy/helm/rapa/"}


def test_every_capture_is_used() -> None:
    captures = json.loads(CAPTURES.read_text(encoding="utf-8"))["capturas"]
    used = {src for src, _ in manual().images}
    missing = [c["arquivo"] for c in captures if f"img/{c['arquivo']}" not in used]
    assert not missing, f"capturas que o manual não usa: {missing}"
