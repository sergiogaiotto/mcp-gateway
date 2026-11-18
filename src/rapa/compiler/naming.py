"""Nomes, ids e títulos em pt-BR, gerados de forma determinística."""

import re
import unicodedata
from urllib.parse import urlsplit

__all__ = [
    "IdAllocator",
    "article",
    "clip_label",
    "lower_first",
    "name_from",
    "page_phrase",
    "quoted",
    "slugify",
    "strip_accents",
]

_MAX_ID = 40
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
#: Palavras femininas comuns em rótulos de formulário (o resto segue a terminação).
_FEMININE = frozenset(
    {
        "agencia",
        "categoria",
        "chave",
        "cidade",
        "competencia",
        "conta",
        "data",
        "declaracao",
        "descricao",
        "empresa",
        "inscricao",
        "matricula",
        "modalidade",
        "natureza",
        "observacao",
        "opcao",
        "placa",
        "quantidade",
        "razao",
        "rua",
        "senha",
        "serie",
        "situacao",
        "uf",
        "unidade",
    }
)
_MASCULINE = frozenset({"cep", "cnpj", "cpf", "dia", "email", "e-mail", "mapa", "nome", "rg"})


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def slugify(text: str, *, max_words: int = 4, max_len: int = 36) -> str:
    """``"Código de verificação"`` → ``"codigo_de_verificacao"`` (só a-z, 0-9 e _)."""
    words = [w for w in _NON_ALNUM.split(strip_accents(text).lower()) if w][:max_words]
    slug = ""
    for word in words:
        candidate = f"{slug}_{word}" if slug else word
        if len(candidate) > max_len:
            break
        slug = candidate
    if not slug and words:
        slug = words[0][:max_len]
    return slug


def clip_label(text: str, limit: int = 60) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0] or text[: limit - 1]
    return cut + "…"


def quoted(text: str, limit: int = 60) -> str:
    return f"“{clip_label(text, limit)}”"


def _is_acronym(word: str) -> bool:
    letters = [c for c in word if c.isalpha()]
    return len(letters) >= 2 and all(c.isupper() for c in letters)


def lower_first(label: str) -> str:
    """Põe a primeira palavra em minúscula, exceto siglas (CPF, UF)."""
    if not label:
        return label
    first, _, rest = label.partition(" ")
    if _is_acronym(first):
        return label
    return first[:1].lower() + first[1:] + (" " + rest if rest else "")


def article(label: str) -> str:
    """Artigo definido para o rótulo ("o CPF", "a senha", "a UF")."""
    first = strip_accents(label.split(" ", 1)[0]).lower().strip(".:")
    if first in _MASCULINE:
        return "o"
    if first in _FEMININE:
        return "a"
    if _is_acronym(label.split(" ", 1)[0]):
        return "o"
    if first.endswith(("a", "cao", "sao", "dade", "gem", "ie", "ude")):
        return "a"
    return "o"


def page_phrase(url: str) -> tuple[str, str]:
    """Descrição curta da página pelo caminho: ("a página de login", "login")."""
    path = urlsplit(url).path.strip("/")
    if not path:
        return "a página inicial", "inicio"
    segments = [s for s in path.split("/") if s]
    for segment in reversed(segments):
        words = [w for w in re.split(r"[-_.]+", segment) if w]
        if words and all(w.isalpha() for w in words):
            readable = " ".join(words).lower()
            if readable in ("index", "home", "default"):
                return "a página inicial", "inicio"
            return f"a página de {readable}", slugify(readable)
    return "a página", "pagina"


class IdAllocator:
    """Ids de passo únicos no formato ``^[a-z][a-z0-9_]{0,39}$``."""

    def __init__(self, taken: set[str] | None = None) -> None:
        self._taken = set(taken or ())

    def allocate(self, base: str) -> str:
        slug = slugify(base, max_words=6, max_len=_MAX_ID - 4).strip("_") or "passo"
        if not slug[0].isalpha():
            slug = f"passo_{slug}"[: _MAX_ID - 4].rstrip("_")
        candidate = slug
        n = 2
        while candidate in self._taken:
            candidate = f"{slug}_{n}"
            n += 1
        self._taken.add(candidate)
        return candidate


def name_from(text: str, fallback: str) -> str:
    """Nome técnico (``^[a-z_][a-z0-9_]{0,63}$``) a partir de um rótulo."""
    slug = slugify(text, max_words=4, max_len=40)
    if not slug:
        return fallback
    if slug[0].isdigit():
        slug = f"{fallback}_{slug}"[:60]
    return slug
