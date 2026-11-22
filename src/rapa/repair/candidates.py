"""Candidatos para o alvo do passo que quebrou, a partir do retrato da página.

Regras (todas determinísticas, sem navegador e sem modelo de linguagem):

- **Só o retrato.** Os candidatos saem do retrato que o executor tirou na falha
  (``rapa.core.snapshot``). Nada é executado de novo para montá-los: nenhum passo,
  muito menos um que grava, roda para conserto.
- **O tipo do passo manda.** Um clique só aceita botões, links, itens de menu, abas,
  opções e caixas; digitar, só campos de texto (nunca caixas, arquivos ou botões);
  escolher, só ``<select>``; marcar, só caixas e opções; anexar, só
  ``<input type=file>``; ler uma tabela, só tabelas. Passos que agem pedem elementos
  habilitados, e só um passo irreversível aceita um botão ou link com verbo
  irreversível (emitir, enviar, concluir, excluir...): o efeito do passo não muda.
- **Credenciais só no lugar certo.** O passo que digita a senha do cofre só aceita um
  campo que o gravador reconheceria como a senha de um login (``credential_kind``:
  ``type=password`` de verdade, que não é senha nova nem de uma operação); o usuário,
  só o campo de usuário; o código de uso único, só o campo de código. Um passo que
  digita um texto nunca vai para o campo da senha de um login.
- **Localizadores do compilador.** O alvo de cada candidato é montado por
  ``rapa.compiler.locators.build_target`` (papel e nome, rótulo, placeholder, test
  id, texto, id estável, ``name``), com as contagens do retrato: só entram
  localizadores únicos no documento. O candidato que só teria o caminho CSS, ou que
  só é único dentro de uma linha quando o passo não escolhia linha, fica de fora.
  Num passo que age dentro de uma linha (``within`` e ``within_has_text``, como o
  "Baixar" de cada nota), a linha continua a mesma de antes e só entram papel e nome
  ou texto únicos dentro da linha do retrato, que não repetem a chave dela (o
  localizador precisa servir para todas as linhas).
- **Nota.** De 0 a 100: o mesmo papel do localizador antigo (ou do substantivo da
  intenção, "botão", "campo"...), as palavras em comum com a intenção e os
  localizadores antigos (sem acentos, com alguns sinônimos comuns de portais:
  entrar e acessar, baixar e download, pesquisar e buscar...), um texto igual a um
  dos antigos, o mesmo iframe, um localizador único e o elemento habilitado. Os
  candidatos vêm da maior nota para a menor e, no empate, na ordem da página; os
  números (``c1``, ``c2``...) seguem essa ordem.
"""

import re
import unicodedata
from collections.abc import Iterable
from typing import Any

from pydantic import ValidationError

from rapa.compiler.locators import (
    TargetError,
    build_target,
    element_label,
    element_noun,
    frame_selectors,
)
from rapa.compiler.naming import quoted
from rapa.core import templates
from rapa.core.contracts import RepairCandidate, RepairStepInfo
from rapa.core.robot import (
    ARIA_ROLES,
    CheckStep,
    ClickStep,
    DownloadStep,
    Effect,
    ExtractTableStep,
    ExtractTextStep,
    FillStep,
    Locator,
    LocatorBy,
    Manifest,
    PressStep,
    SecretValue,
    SelectStep,
    Step,
    Target,
    UploadStep,
    WaitForStep,
)
from rapa.core.snapshot import (
    KIND_ROLES,
    REDACTED,
    PageSnapshot,
    SnapshotElement,
    SnapshotFrameInfo,
)
from rapa.recorder.models import ElementDescriptor, FrameInfo, LocatorCounts, RowInfo
from rapa.recorder.sensitivity import credential_kind, irreversible_click

__all__ = [
    "DEFAULT_CANDIDATES",
    "MAX_CANDIDATES",
    "NotRepairable",
    "apply_candidate",
    "enumerate_candidates",
    "repair_target",
    "step_info",
]

#: Quantos candidatos a tela mostra (e o modelo vê), por padrão.
DEFAULT_CANDIDATES = 8
MAX_CANDIDATES = 20

_ACTION_STEPS = (ClickStep, FillStep, SelectStep, CheckStep, UploadStep, PressStep, DownloadStep)
_TEXT_FIELD_TAGS = frozenset({"input", "textarea"})
_NOT_TEXT_INPUTS = frozenset(
    {"checkbox", "radio", "file", "submit", "button", "reset", "image", "range", "color", "hidden"}
)
_CLICKABLE_TEXT_ROLES = frozenset({"button", "link", "menuitem", "tab", "option"})
_SECRET_FIELDS = {"password": "password", "username": "username", "totp": "code"}
#: Substantivos das intenções que o compilador escreve ("botão “Entrar”") e o papel deles.
_NOUN_ROLES = {
    "botao": "button",
    "link": "link",
    "campo": "textbox",
    "lista": "combobox",
    "caixa": "checkbox",
    "opcao": "radio",
    "aba": "tab",
    "tabela": "table",
    "titulo": "heading",
}
_GENERIC = frozenset(
    {
        *_NOUN_ROLES,
        "elemento",
        "item",
        "menu",
        "texto",
        "selecao",
        "de",
        "do",
        "da",
        "dos",
        "das",
        "o",
        "a",
        "os",
        "as",
        "e",
        "em",
        "no",
        "na",
        "nos",
        "nas",
        "um",
        "uma",
        "para",
        "por",
        "com",
        "ao",
        "the",
        "of",
        "linha",
    }
)
#: Sinônimos comuns em portais (sem acentos e em minúsculas).
_SYNONYMS: tuple[frozenset[str], ...] = (
    frozenset({"entrar", "acessar", "login", "logar", "logon", "signin", "conectar"}),
    frozenset({"sair", "logout", "desconectar", "encerrar"}),
    frozenset({"baixar", "download", "descarregar"}),
    frozenset({"pesquisar", "buscar", "procurar", "consultar", "filtrar", "search"}),
    frozenset({"enviar", "submeter", "transmitir", "submit"}),
    frozenset({"salvar", "gravar", "save"}),
    frozenset({"excluir", "apagar", "remover", "deletar", "delete"}),
    frozenset({"confirmar", "ok", "prosseguir"}),
    frozenset({"proximo", "proxima", "avancar", "seguinte", "next", "continuar"}),
    frozenset({"anterior", "voltar", "back"}),
    frozenset({"usuario", "login", "user", "username"}),
    frozenset({"senha", "password"}),
    frozenset({"emitir", "gerar"}),
    frozenset({"inicio", "home"}),
)
_WORD = re.compile(r"[a-z0-9]+")
_ID_IN_CSS = re.compile(r"#([A-Za-z][A-Za-z0-9_-]*)|\[(?:id|name)=\"?([^\]\"]+)\"?\]")
_MAX_VALUE = 500
_MAX_NAME = 300


class NotRepairable(ValueError):
    """O passo não pode ser consertado assim (mensagem em pt-BR)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# ---------------------------------------------------------------------------
# O passo
# ---------------------------------------------------------------------------


def repair_target(step: Step) -> Target:
    """O alvo do passo (que o conserto troca); ``NotRepairable`` se ele não tem um.

    Um alvo cujos localizadores dependem de um valor da execução (``{{ item.numero }}``,
    ``{{ inputs.uf }}``) também fica de fora: os candidatos saem dos textos do
    retrato, que são os de um valor só, e o passo passaria a agir sempre naquele.
    """
    target = getattr(step, "target", None)
    if not isinstance(target, Target):
        raise NotRepairable(
            f"o passo '{step.id}' ({step.kind}) não tem um elemento-alvo para consertar"
        )
    for locator in target.locators:
        for text in (locator.value, locator.name):
            try:
                dynamic = bool(text) and templates.has_references(text or "")
            except templates.TemplateError:
                dynamic = True
            if dynamic:
                raise NotRepairable(
                    f"o alvo do passo '{step.id}' depende de um valor da execução (entrada, "
                    "variável ou item da repetição); um alvo tirado do retrato serviria só "
                    "para o valor daquela execução: grave o passo de novo"
                )
    return target


def step_info(step: Step) -> RepairStepInfo:
    target = repair_target(step)
    return RepairStepInfo(
        step_id=step.id,
        title=step.title,
        kind=step.kind,
        effect=step.effect,
        intent=target.intent,
        locators=list(target.locators),
        frames=list(target.frames),
        within=target.within,
        within_has_text=target.within_has_text,
    )


# ---------------------------------------------------------------------------
# Texto e nota
# ---------------------------------------------------------------------------


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def _literal(text: str) -> str:
    """Só o trecho literal de um template (as referências não contam)."""
    try:
        parts = templates.parse(text)
    except templates.TemplateError:
        return ""
    return " ".join(part for part in parts if isinstance(part, str))


def _words(text: str | None) -> list[str]:
    if not text:
        return []
    return [w for w in _WORD.findall(_fold(text)) if w not in _GENERIC]


def _keys(word: str) -> set[str]:
    keys = {word}
    for index, group in enumerate(_SYNONYMS):
        if word in group:
            keys.add(f"#{index}")
    return keys


def _old_texts(target: Target) -> list[str]:
    """Os textos do alvo antigo: a intenção e os valores dos localizadores."""
    texts = [_literal(target.intent)]
    for locator in target.locators:
        if locator.by is LocatorBy.ROLE:
            if locator.name:
                texts.append(_literal(locator.name))
        elif locator.by in (LocatorBy.CSS, LocatorBy.XPATH):
            for match in _ID_IN_CSS.finditer(locator.value):
                texts.append(match.group(1) or match.group(2) or "")
        else:
            texts.append(_literal(locator.value))
    return [text for text in texts if text]


def _old_role(target: Target) -> str | None:
    for locator in target.locators:
        if locator.by is LocatorBy.ROLE:
            return locator.value
    words = _WORD.findall(_fold(target.intent))
    return _NOUN_ROLES.get(words[0]) if words else None


def _candidate_texts(element: SnapshotElement) -> list[str]:
    return [
        text
        for text in (
            element.name,
            element.label,
            element.placeholder,
            element.text,
            element.test_id,
            element.title,
            element.id,
            element.name_attr,
        )
        if text
    ]


def _score(element: SnapshotElement, old: Target, frames: list[str]) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    role = _old_role(old)
    if role is not None and element.role == role:
        score += 30
        reasons.append(f"mesmo papel de antes ({role})")
    old_texts = _old_texts(old)
    old_words = {word for text in old_texts for word in _words(text)}
    candidate_texts = _candidate_texts(element)
    candidate_keys: set[str] = set()
    for text in candidate_texts:
        for word in _words(text):
            candidate_keys |= _keys(word)
    if old_words:
        matched = sum(1 for word in old_words if _keys(word) & candidate_keys)
        if matched:
            score += round(40 * matched / len(old_words))
            reasons.append("palavras em comum com o alvo de antes")
    folded_old = {" ".join(_fold(text).split()) for text in old_texts}
    if any(" ".join(_fold(text).split()) in folded_old for text in candidate_texts):
        score += 15
        reasons.append("um texto igual ao de antes")
    if frames == list(old.frames):
        score += 5
    else:
        reasons.append("em outro quadro (iframe)")
    counts = element.counts
    if 1 in (
        counts.role_name,
        counts.label,
        counts.placeholder,
        counts.test_id,
        counts.text,
        counts.id,
        counts.name_attr,
    ):
        score += 5
    if element.enabled:
        score += 5
    return min(score, 100), reasons


# ---------------------------------------------------------------------------
# Elementos que servem ao passo
# ---------------------------------------------------------------------------


def _css_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _frame_info(info: SnapshotFrameInfo) -> FrameInfo:
    """O iframe do retrato, para os seletores do compilador.

    O endereço do retrato perdeu a consulta e o fragmento, e o iframe da página ainda os
    tem: o seletor pelo endereço aceita o caminho sozinho ou seguido deles. Um endereço
    com um valor removido (``REMOVIDO``) não identifica o iframe.
    """
    src = info.src if info.src and REDACTED not in info.src else None
    tag = info.tag if info.tag in ("iframe", "frame") else "iframe"
    css = (
        ",".join(
            f"{tag}[src{op}{_css_quote(value)}]"
            for op, value in (("=", src), ("^=", f"{src}?"), ("^=", f"{src}#"))
        )
        if src
        else None
    )
    return FrameInfo(tag=info.tag, id=info.id, name=info.name, title=info.title, src=src, css=css)


def _descriptor(element: SnapshotElement, snapshot: PageSnapshot) -> ElementDescriptor | None:
    if element.frame >= len(snapshot.frames):
        return None
    frame = snapshot.frames[element.frame]
    row = element.row
    try:
        return ElementDescriptor(
            tag=element.tag,
            input_type=element.input_type,
            role=element.role,
            name=element.name,
            label=element.label,
            placeholder=element.placeholder,
            test_id=element.test_id,
            name_attr=element.name_attr,
            id=element.id,
            # Sem caminho CSS: o candidato precisa de um localizador estável.
            css="",
            text=element.text,
            title=element.title,
            autocomplete=element.autocomplete,
            frame_path=[_frame_info(i) for i in frame.path],
            shadow_path=["(componente)"] if element.in_shadow else None,
            is_password=element.is_password,
            is_one_time_code=element.is_one_time_code,
            sensitive=element.sensitive,
            row=RowInfo(
                text=row.text,
                cells=list(row.cells),
                key=row.key,
                role_name_count=row.role_name_count,
                text_count=row.text_count,
            )
            if row is not None
            else None,
            counts=LocatorCounts(**element.counts.model_dump()),
        )
    except ValidationError:
        return None


def _fits(step: Step, element: SnapshotElement, descriptor: ElementDescriptor) -> bool:
    role = element.role or ""
    if isinstance(step, _ACTION_STEPS) and not element.enabled:
        return False
    if (
        isinstance(step, _ACTION_STEPS)
        and step.effect is not Effect.IRREVERSIBLE
        and irreversible_click(descriptor)
    ):
        # O compilador faria deste clique um passo irreversível, e o conserto não muda o
        # efeito: um passo que não é irreversível nunca passa para esse botão.
        return False
    match step:
        case ClickStep():
            return role in KIND_ROLES["click"]
        case DownloadStep():
            return role in KIND_ROLES["download"]
        case FillStep(value=value):
            if element.tag not in _TEXT_FIELD_TAGS and role != "textbox":
                return False
            if (element.input_type or "") in _NOT_TEXT_INPUTS:
                return False
            kind = credential_kind(descriptor)
            if isinstance(value, SecretValue):
                return kind == _SECRET_FIELDS[value.field]
            return kind != "password"
        case SelectStep():
            return element.tag == "select"
        case CheckStep():
            return role in KIND_ROLES["check"]
        case UploadStep():
            return element.tag == "input" and element.input_type == "file"
        case PressStep():
            return role in KIND_ROLES["press"]
        case ExtractTableStep():
            return role in KIND_ROLES["extract_table"]
        case ExtractTextStep() | WaitForStep():
            return True
    return False


def _escaped(value: str | None, limit: int) -> str | None:
    if not value or REDACTED in value:
        return None
    escaped = templates.escape(value)
    return escaped if len(escaped) <= limit else None


def _row_locators(element: SnapshotElement) -> list[Locator]:
    """Localizadores que servem a todas as linhas: únicos na linha e sem a chave dela."""
    row = element.row
    if row is None:
        return []
    key = row.key or ""
    locators: list[Locator] = []
    role = element.role
    name = _escaped(element.name, _MAX_NAME)
    if role in ARIA_ROLES and name and row.role_name_count == 1 and (not key or key not in name):
        locators.append(Locator(by=LocatorBy.ROLE, value=role, name=name))
    text = _escaped(element.text, _MAX_VALUE)
    text_ok = (
        text is not None
        and len(element.text or "") <= 100
        and (role in _CLICKABLE_TEXT_ROLES or element.tag in ("a", "button"))
    )
    if text and text_ok and row.text_count == 1 and (not key or key not in text):
        locators.append(Locator(by=LocatorBy.TEXT, value=text))
    return locators


def _target_for(
    step: Step, element: SnapshotElement, descriptor: ElementDescriptor, old: Target
) -> Target | None:
    label = element_label(descriptor)
    if REDACTED in label:
        label = element.role or element.tag
    intent = f"{element_noun(descriptor)} {quoted(label)}"
    if old.within is not None:
        locators = _row_locators(element)
        if not locators:
            return None
        try:
            frames = frame_selectors(descriptor.frame_path)
        except TargetError:
            return None
        return Target(
            intent=f"{intent} da linha"[:300],
            locators=locators,
            frames=frames,
            within=old.within,
            within_has_text=old.within_has_text,
        )
    prefer_label = isinstance(step, (FillStep, SelectStep, UploadStep))
    try:
        built = build_target(descriptor, intent, prefer_label=prefer_label)
    except TargetError:
        return None
    if built.fragile or built.target.within is not None:
        # Só o caminho CSS, ou só único dentro de uma linha: o passo não escolhia linha.
        return None
    return built.target


def enumerate_candidates(
    snapshot: PageSnapshot, step: Step, *, limit: int = DEFAULT_CANDIDATES
) -> list[RepairCandidate]:
    """Os candidatos para o alvo de ``step``, do mais provável para o menos provável."""
    old = repair_target(step)
    if snapshot.step_id != step.id:
        raise NotRepairable("o retrato da página é de outro passo")
    limit = max(1, min(limit, MAX_CANDIDATES))
    found: list[tuple[int, int, RepairCandidate]] = []
    seen: set[str] = set()
    for index, element in enumerate(snapshot.elements):
        descriptor = _descriptor(element, snapshot)
        if descriptor is None or not _fits(step, element, descriptor):
            continue
        target = _target_for(step, element, descriptor, old)
        if target is None:
            continue
        key = target.model_dump_json(include={"locators", "frames", "within", "within_has_text"})
        if key in seen:
            continue
        seen.add(key)
        score, reasons = _score(element, old, list(target.frames))
        found.append(
            (
                score,
                index,
                RepairCandidate(
                    candidate_id="c0",
                    description=target.intent,
                    role=element.role,
                    tag=element.tag,
                    name=element.name,
                    label=element.label,
                    placeholder=element.placeholder,
                    text=element.text,
                    test_id=element.test_id,
                    row_text=element.row.text[:300] if element.row is not None else None,
                    frames=list(target.frames),
                    sensitive=element.sensitive,
                    enabled=element.enabled,
                    score=score,
                    reasons=reasons,
                    target=target,
                ),
            )
        )
    found.sort(key=lambda item: (-item[0], item[1]))
    return [
        candidate.model_copy(update={"candidate_id": f"c{position}"})
        for position, (_, _, candidate) in enumerate(found[:limit], start=1)
    ]


# ---------------------------------------------------------------------------
# A versão nova
# ---------------------------------------------------------------------------


def _iter_dicts(steps: Iterable[Any]) -> Iterable[dict[str, Any]]:
    for step in steps:
        if not isinstance(step, dict):
            continue
        yield step
        for key in ("steps", "then", "otherwise"):
            child = step.get(key)
            if isinstance(child, list):
                yield from _iter_dicts(child)


def apply_candidate(manifest: Manifest, step_id: str, candidate: RepairCandidate) -> Manifest:
    """O manifesto com o alvo do passo ``step_id`` trocado pelo do candidato, e só isso.

    Nada mais muda: nem o efeito, nem o valor, nem os outros passos. O resultado é
    validado de novo; o portão de política roda na criação da versão.
    """
    original = manifest.step_by_id(step_id)
    if original is None:
        raise NotRepairable(f"o passo '{step_id}' não existe nesta versão")
    repair_target(original)
    data = manifest.model_dump(mode="json")
    step = next((s for s in _iter_dicts(data["steps"]) if s.get("id") == step_id), None)
    if step is None:  # pragma: no cover - conferido acima
        raise NotRepairable(f"o passo '{step_id}' não existe nesta versão")
    step["target"] = candidate.target.model_dump(mode="json", exclude_none=True)
    try:
        repaired = Manifest.model_validate(data)
    except ValidationError as exc:
        raise NotRepairable(f"o alvo novo não é válido: {exc.errors()[0]['msg']}") from None
    after = repaired.step_by_id(step_id)
    if after is None or after.effect is not original.effect:  # pragma: no cover - defensivo
        raise NotRepairable("o conserto não pode mudar o efeito do passo")
    return repaired
