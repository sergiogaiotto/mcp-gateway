"""Escolha de localizadores a partir do descritor gravado.

Ordem de preferência: papel + nome, rótulo, placeholder, test id, texto (links
e botões), CSS por id estável e CSS por atributo ``name``. Só entram
localizadores que acharam exatamente um elemento na gravação; quando o papel e
o nome se repetem numa tabela (por exemplo, um link "Baixar" por linha), o alvo
passa a ser procurado dentro da linha que tem um texto exclusivo.
"""

import re
from dataclasses import dataclass, field

from rapa.core import templates
from rapa.core.robot import ARIA_ROLES, Locator, LocatorBy, Target
from rapa.recorder.models import REDACTED, ElementDescriptor, FrameInfo

__all__ = [
    "BuiltTarget",
    "TargetError",
    "build_target",
    "element_label",
    "element_noun",
    "frame_selectors",
    "is_stable_id",
]

_GENERATED_ID = re.compile(
    r"(^|[-_:])(ember|ext-gen|ext-comp|yui|gwt-uid|j_idt|react-select|mui|radix|headlessui"
    r"|__bvid__|vaadin|cdk|mat-|ui-id|tippy|rc-|rc_)"
    r"|:|[0-9]{3,}|[0-9a-f]{8,}|^[0-9]",
    re.IGNORECASE,
)
_CSS_IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_CLICKABLE_TEXT_ROLES = frozenset({"button", "link", "menuitem", "tab", "option"})
_MAX_FRAMES = 4
_MAX_LOCATOR_VALUE = 500
_MAX_LOCATOR_NAME = 300


class TargetError(ValueError):
    """Não foi possível montar o alvo (mensagem em pt-BR)."""


def is_stable_id(value: str | None) -> bool:
    """Rejeita ids que parecem gerados (sequências longas de dígitos ou hexadecimais,
    prefixos de frameworks, ``:r1:`` do React)."""
    return bool(value) and len(value or "") <= 40 and not _GENERATED_ID.search(value or "")


def _css_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _id_selector(value: str) -> str:
    return f"#{value}" if _CSS_IDENT.fullmatch(value) else f"[id={_css_string(value)}]"


def element_noun(element: ElementDescriptor) -> str:
    """Substantivo em pt-BR para o tipo de elemento."""
    if element.tag == "input" and element.input_type == "file":
        return "campo de arquivo"
    match element.role:
        case "button":
            return "botão"
        case "link":
            return "link"
        case "textbox" | "searchbox" | "spinbutton":
            return "campo"
        case "combobox" | "listbox":
            return "lista" if element.tag == "select" else "campo"
        case "checkbox" | "switch":
            return "caixa de seleção"
        case "radio":
            return "opção"
        case "tab":
            return "aba"
        case "menuitem":
            return "item de menu"
    if element.tag in ("input", "textarea"):
        return "campo"
    if element.tag == "select":
        return "lista"
    return "elemento"


def element_label(element: ElementDescriptor) -> str:
    """O texto que a pessoa vê para o elemento."""
    for candidate in (
        element.name,
        element.label,
        element.placeholder,
        element.text,
        element.title,
        element.name_attr,
        element.id,
    ):
        if candidate and candidate.strip():
            return " ".join(candidate.split())
    return element.tag


def frame_selectors(path: list[FrameInfo] | None) -> list[str]:
    """Seletores CSS dos iframes, do mais externo ao mais interno."""
    selectors: list[str] = []
    for info in path or []:
        tag = info.tag if info.tag in ("iframe", "frame") else "iframe"
        if info.id and is_stable_id(info.id):
            selectors.append(_id_selector(info.id))
        elif info.name:
            selectors.append(f"{tag}[name={_css_string(info.name)}]")
        elif info.title:
            selectors.append(f"{tag}[title={_css_string(info.title)}]")
        elif info.css:
            selectors.append(info.css)
        elif info.src:
            selectors.append(f"{tag}[src={_css_string(info.src)}]")
        else:
            raise TargetError("não foi possível identificar um dos iframes do elemento")
    if len(selectors) > _MAX_FRAMES:
        raise TargetError(f"o elemento está em mais de {_MAX_FRAMES} iframes aninhados")
    return selectors


@dataclass
class BuiltTarget:
    target: Target
    warnings: list[str] = field(default_factory=list)
    #: Só sobrou o caminho CSS gravado (frágil).
    fragile: bool = False


def _verdict(js: int | None, pw: int | None) -> tuple[bool, bool]:
    """(usar, rebaixar) a partir da contagem do script e da conferência do Playwright.

    A contagem do script foi feita no instante do evento, no documento certo; a do
    Playwright vem logo depois e confirma que o localizador funciona como o
    executor vai usá-lo. Repetido no instante do evento é sempre descartado.
    """
    if js is not None and js > 1:
        return False, False
    if pw is not None:
        if pw > 1:
            return False, False
        if pw == 1:
            return True, False
        # O Playwright não achou: pode ter sido a página mudando durante a conferência.
        return js == 1, True
    if js is None:
        # Página grande demais para contar: entra, mas depois dos localizadores conferidos.
        return True, True
    return js == 1, False


def _loc(by: LocatorBy, value: str | None, name: str | None = None) -> Locator | None:
    """Localizador com os textos já escapados, ou None se não couber no formato.

    Um texto (nome, rótulo, placeholder, texto visível) com um valor sigiloso
    apagado pela limpeza da gravação (``REMOVIDO``) nunca aparece assim na página:
    não vira localizador.
    """
    if not value:
        return None
    if by is not LocatorBy.CSS and (REDACTED in value or (name is not None and REDACTED in name)):
        return None
    value = templates.escape(value)
    escaped_name = templates.escape(name) if name else None
    if len(value) > _MAX_LOCATOR_VALUE or (escaped_name and len(escaped_name) > _MAX_LOCATOR_NAME):
        return None
    return Locator(by=by, value=value, name=escaped_name)


def _strategies(
    element: ElementDescriptor,
) -> dict[str, tuple[Locator | None, int | None, int | None, int | None]]:
    """Cada estratégia: (localizador, contagem do script, do Playwright, dentro da linha)."""
    probe = element.probe if element.probe is not None and element.probe.found else None
    counts = element.counts
    row = element.row
    role = element.role
    text_ok = bool(
        element.text
        and len(element.text) <= 100
        and (role in _CLICKABLE_TEXT_ROLES or element.tag in ("a", "button"))
    )
    stable_id = element.id if element.id and is_stable_id(element.id) else None
    return {
        "role": (
            _loc(LocatorBy.ROLE, role, element.name)
            if role in ARIA_ROLES and element.name
            else None,
            counts.role_name,
            probe.role_name if probe else None,
            row.role_name_count if row else None,
        ),
        "label": (
            _loc(LocatorBy.LABEL, element.label),
            counts.label,
            probe.label if probe else None,
            None,
        ),
        "placeholder": (
            _loc(LocatorBy.PLACEHOLDER, element.placeholder),
            counts.placeholder,
            probe.placeholder if probe else None,
            None,
        ),
        "test_id": (_loc(LocatorBy.TEST_ID, element.test_id), counts.test_id, None, None),
        "text": (
            _loc(LocatorBy.TEXT, element.text) if text_ok else None,
            counts.text,
            probe.text if probe else None,
            row.text_count if row else None,
        ),
        "id": (
            _loc(LocatorBy.CSS, _id_selector(stable_id)) if stable_id else None,
            counts.id,
            None,
            None,
        ),
        "name_attr": (
            _loc(LocatorBy.CSS, f"{element.tag}[name={_css_string(element.name_attr)}]")
            if element.name_attr
            else None,
            counts.name_attr,
            None,
            None,
        ),
    }


_ORDER = ("role", "label", "placeholder", "test_id", "text", "id", "name_attr")
_ORDER_LABEL_FIRST = ("label", "role", "placeholder", "test_id", "text", "id", "name_attr")


def build_target(
    element: ElementDescriptor, intent: str, *, prefer_label: bool = False
) -> BuiltTarget:
    """Monta o alvo do passo. Levanta ``TargetError`` se os iframes não puderem ser descritos."""
    strategies = _strategies(element)
    row = element.row
    role_locator, role_count, _, row_role_count = strategies["role"]
    # Papel e nome repetidos no documento, mas únicos numa linha com texto exclusivo.
    use_row = bool(
        role_locator is not None
        and role_count is not None
        and role_count > 1
        and row is not None
        and row.key
        and row_role_count == 1
    )
    preferred: list[Locator] = []
    demoted: list[Locator] = []
    for key in _ORDER_LABEL_FIRST if prefer_label else _ORDER:
        locator, js, pw, row_js = strategies[key]
        if locator is None:
            continue
        if use_row and row_js is not None:
            if row_js == 1:
                preferred.append(locator)
            continue
        use, lower = _verdict(js, pw)
        if use:
            (demoted if lower else preferred).append(locator)
    ordered = preferred + demoted

    warnings: list[str] = []
    fragile = False
    if not ordered:
        fragile = True
        fallback = _loc(LocatorBy.CSS, element.css)
        if fallback is None:
            raise TargetError(f"não há como localizar {intent}")
        ordered = [fallback]
        warnings.append(
            f"Nenhum localizador estável para {intent}; o robô usa o caminho CSS gravado, "
            "que pode quebrar se a página mudar."
        )
    try:
        frames = frame_selectors(element.frame_path)
    except TargetError as exc:
        raise TargetError(f"{intent}: {exc}") from None
    within: Locator | None = None
    within_text: str | None = None
    if use_row and row is not None and row.key:
        within = Locator(by=LocatorBy.ROLE, value="row")
        within_text = templates.escape(row.key)
    target = Target(
        intent=intent[:300],
        locators=ordered[:8],
        frames=frames,
        within=within,
        within_has_text=within_text,
        snapshot=_snapshot(element)[:4000] or None,
    )
    return BuiltTarget(target=target, warnings=warnings, fragile=fragile)


def _snapshot(element: ElementDescriptor) -> str:
    """Trecho no estilo da árvore de acessibilidade, só com o que a página mostra."""
    lines: list[str] = []
    indent = ""
    for info in element.frame_path or []:
        label = info.title or info.name or info.id or "quadro"
        lines.append(f'{indent}- iframe "{label}":')
        indent += "  "
    if element.row is not None:
        lines.append(f'{indent}- row "{element.row.text[:300]}":')
        indent += "  "
    role = element.role or element.tag
    name = element.name or element.label or element.text or ""
    lines.append(f'{indent}- {role} "{name}"' if name else f"{indent}- {role}")
    return "\n".join(lines)
