"""Retrato da página no passo que falhou, para o conserto assistido.

O executor chama :func:`capture` uma vez, quando um passo com alvo falha porque o
elemento não foi encontrado, foi encontrado mais de uma vez ou só por um localizador
alternativo num passo que grava (``rapa.core.snapshot.SNAPSHOT_ERROR_CODES``). O
retrato só lê a página: o script (``snapshot.js``) roda uma vez em cada quadro, sem
mandar nenhum evento, foco ou rolagem, e nunca lê o valor de um campo. Nada é
repetido nem clicado para montar os candidatos do conserto, que saem só do retrato.

A limpeza vem em duas camadas. O script só devolve o que a página mostra (papel,
nome, rótulo, placeholder, texto, ids e a linha de tabela), sem valores. Aqui, todo
texto perde os valores das credenciais digitadas e das entradas da execução
(trocados por ``REMOVIDO``, sem distinguir maiúsculas), os endereços perdem a
consulta, o fragmento e o usuário, e cada campo ganha as marcas de sigilo do
gravador (``rapa.recorder.sensitivity``: senha, código de uso único, segredo de uma
operação ou nome com cara de segredo). Tudo é cortado nos limites de
:class:`~rapa.core.snapshot.PageSnapshot`; o que não passa na validação fica de fora.

É melhor esforço: um erro ou o fim do prazo (``CAPTURE_TIMEOUT_S``) deixa a execução
sem retrato, nunca muda o resultado dela.
"""

import asyncio
import re
import time
from collections.abc import Iterable, Mapping
from importlib.resources import files
from typing import Any
from urllib.parse import quote, quote_plus, urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Frame, Page
from pydantic import ValidationError

from rapa.core import templates
from rapa.core.snapshot import (
    MAX_SNAPSHOT_BYTES,
    MAX_SNAPSHOT_ELEMENTS,
    MAX_SNAPSHOT_FRAMES,
    REDACTED,
    PageSnapshot,
    SnapshotElement,
    SnapshotFrame,
    SnapshotFrameInfo,
    snapshot_roles,
)
from rapa.recorder.models import ElementDescriptor
from rapa.recorder.sensitivity import is_secret_field, is_transaction_secret, looks_secret

__all__ = ["CAPTURE_TIMEOUT_S", "Masker", "capture", "fit", "flatten_strings", "page_path"]

_SNAPSHOT_JS = files("rapa.runner").joinpath("snapshot.js").read_text(encoding="utf-8")
#: Prazo do retrato inteiro (todos os quadros).
CAPTURE_TIMEOUT_S = 4.0
_MAX_FRAME_DEPTH = 4
#: Valores mais curtos que isto mascarariam texto comum demais.
_MIN_MASKED = 4
#: Quanto o script devolve além de cada limite (``SLACK`` em snapshot.js): os valores
#: saem antes do corte, e um valor que cai no limite nunca fica pela metade. Um valor
#: mais longo que isto também sai pelo começo dele.
_SLACK = 200
_FIELD_TAGS = frozenset({"input", "textarea", "select"})
#: Tipos de passo em que o alvo pode ser um elemento só pelo ``data-testid`` (um texto).
_TEXT_KINDS = frozenset({"extract_text", "wait_for"})
#: Folga sob o limite do plano de controle (o JSON do resultado tem outros campos).
_TARGET_BYTES = MAX_SNAPSHOT_BYTES * 9 // 10
_FRAME_INFO_JS = """(el) => ({
  tag: String(el.tagName || 'iframe').toLowerCase(),
  id: el.getAttribute('id'),
  name: el.getAttribute('name'),
  title: el.getAttribute('title'),
  src: el.getAttribute('src'),
})"""
#: Limites de :class:`~rapa.core.snapshot.SnapshotFrameInfo`, aplicados de novo depois da
#: máscara (``REMOVIDO`` pode ser mais longo que o valor que troca).
_TEXT_FIELDS = (
    "name",
    "label",
    "placeholder",
    "test_id",
    "name_attr",
    "id",
    "title",
    "text",
    "autocomplete",
)


def _forms(value: str) -> set[str]:
    """O valor como ele pode aparecer no retrato: como veio, com os brancos juntados (como
    fica todo texto da página) e codificado num endereço (como o executor e o navegador o
    põem no caminho)."""
    forms = {value, " ".join(value.split())}
    for safe in ("", "/", "!~*'()"):
        try:
            forms.add(quote(value, safe=safe))
        except UnicodeError:
            pass
    try:
        forms.add(quote_plus(value))
    except UnicodeError:
        pass
    return forms


class Masker:
    """Troca valores sigilosos por ``REMOVIDO`` em qualquer texto, sem distinguir caixa.

    Com ``deadline`` (``time.monotonic()``), um texto limpo depois dele levanta
    :class:`TimeoutError`: a limpeza roda sem nenhum ``await`` e, com muitas entradas,
    passaria do prazo do retrato sem que ninguém a interrompesse.
    """

    def __init__(self, values: Iterable[str], *, deadline: float | None = None) -> None:
        masked: set[str] = set()
        for value in values:
            for form in _forms(value):
                if len(form) >= _MIN_MASKED:
                    masked.add(form)
                    masked.add(form[:_SLACK])
        unique = sorted(masked, key=len, reverse=True)
        self._pattern = (
            re.compile("|".join(re.escape(v) for v in unique), re.IGNORECASE) if unique else None
        )
        self._deadline = deadline

    def text(self, value: str) -> str:
        if self._pattern is None:
            return value
        if self._deadline is not None and time.monotonic() > self._deadline:
            raise TimeoutError("o prazo do retrato acabou")
        return self._pattern.sub(REDACTED, value)

    def clip(self, value: Any, limit: int) -> str | None:
        """O texto sem os valores, cortado em ``limit`` só depois da limpeza."""
        text = _clip(value, limit + _SLACK)
        return (self.text(text)[:limit] or None) if text else None

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Mapping):
            return {key: self.value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.value(item) for item in value]
        return value


def flatten_strings(value: Any, *, depth: int = 0) -> list[str]:
    """Todos os textos de uma entrada (também os de listas e dos itens delas).

    Um número entra como o executor o digita e o põe num endereço
    (:func:`rapa.core.templates.format_value`).
    """
    if depth > 8:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return [templates.format_value(value)]
        except templates.TemplateError:
            return []
    if isinstance(value, Mapping):
        return [s for item in value.values() for s in flatten_strings(item, depth=depth + 1)]
    if isinstance(value, (list, tuple)):
        return [s for item in value for s in flatten_strings(item, depth=depth + 1)]
    return []


def _frame_src(src: str) -> str | None:
    """O ``src`` de um iframe como o seletor do quadro o usa: sem consulta nem fragmento.

    Um endereço com usuário ou senha e um que não é http(s) (``data:``, ``blob:``...)
    ficam de fora: o primeiro guarda uma credencial, o outro guarda a própria página.
    """
    try:
        parts = urlsplit(src)
        has_userinfo = parts.username is not None or parts.password is not None
    except ValueError:
        return None
    if parts.scheme and parts.scheme not in ("http", "https"):
        return None
    if has_userinfo:
        return None
    return parts._replace(query="", fragment="").geturl() or None


def page_path(url: str | None) -> str:
    """Origem e caminho do endereço: sem usuário, consulta nem fragmento."""
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https"):
        return parts.scheme + ":" if parts.scheme else ""
    host = parts.hostname or ""
    try:
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        port = ""
    return f"{parts.scheme}://{host}{port}{parts.path}"


def _clip(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text[:limit] or None


async def _frame_info(frame: Frame, masker: Masker) -> SnapshotFrameInfo | None:
    try:
        handle = await frame.frame_element()
    except PlaywrightError:
        return None
    try:
        raw = await handle.evaluate(_FRAME_INFO_JS)
    except PlaywrightError:
        return None
    finally:
        try:
            await handle.dispose()
        except PlaywrightError:
            pass
    if not isinstance(raw, Mapping):
        return None
    tag = _clip(raw.get("tag"), 20) or "iframe"
    src = _clip(raw.get("src"), 2000)
    try:
        return SnapshotFrameInfo(
            tag=tag if tag in ("iframe", "frame") else "iframe",
            id=masker.clip(raw.get("id"), 300),
            name=masker.clip(raw.get("name"), 300),
            title=masker.clip(raw.get("title"), 300),
            src=masker.clip(_frame_src(src), 500) if src else None,
        )
    except ValidationError, ValueError:
        return None


async def _frames(
    page: Page, masker: Masker
) -> tuple[list[tuple[Frame, list[SnapshotFrameInfo]]], bool]:
    """Os quadros da página, do principal para dentro, com o caminho dos iframes."""
    found: list[tuple[Frame, list[SnapshotFrameInfo]]] = [(page.main_frame, [])]
    truncated = False
    index = 0
    while index < len(found):
        frame, path = found[index]
        index += 1
        for child in frame.child_frames:
            if child.is_detached():
                continue
            if len(found) >= MAX_SNAPSHOT_FRAMES or len(path) >= _MAX_FRAME_DEPTH:
                truncated = True
                continue
            info = await _frame_info(child, masker)
            if info is None:
                truncated = True
                continue
            found.append((child, [*path, info]))
    return found, truncated


def _sensitive(raw: Mapping[str, Any]) -> bool:
    """Marcas de sigilo do gravador, sobre os mesmos campos que ele olha."""
    tag = raw.get("tag")
    if tag not in _FIELD_TAGS:
        return False
    try:
        element = ElementDescriptor(
            tag=str(tag),
            input_type=raw.get("input_type"),
            role=raw.get("role"),
            name=raw.get("name"),
            label=raw.get("label"),
            placeholder=raw.get("placeholder"),
            test_id=raw.get("test_id"),
            name_attr=raw.get("name_attr"),
            id=raw.get("id"),
            css="",
            title=raw.get("title"),
            text=raw.get("text"),
            autocomplete=raw.get("autocomplete"),
            is_password=bool(raw.get("is_password")),
            is_one_time_code=bool(raw.get("is_one_time_code")),
        )
    except ValidationError:
        return True  # na dúvida, sigiloso
    return is_secret_field(element) or is_transaction_secret(element) or looks_secret(element)


def _element(raw: Any, frame: int, masker: Masker) -> SnapshotElement | None:
    if not isinstance(raw, Mapping):
        return None
    data: dict[str, Any] = {
        "frame": frame,
        "tag": _clip(raw.get("tag"), 40) or "elemento",
        "input_type": _clip(raw.get("input_type"), 40),
        "role": _clip(raw.get("role"), 40),
        "in_shadow": bool(raw.get("in_shadow")),
        "enabled": bool(raw.get("enabled", True)),
        "is_password": bool(raw.get("is_password")),
        "is_one_time_code": bool(raw.get("is_one_time_code")),
    }
    for key in _TEXT_FIELDS:
        data[key] = masker.clip(raw.get(key), 100 if key == "autocomplete" else 300)
    # As marcas de sigilo olham os textos já limpos (os mesmos do retrato).
    data["sensitive"] = _sensitive(data)
    counts = raw.get("counts")
    if isinstance(counts, Mapping):
        data["counts"] = {
            key: value
            for key, value in counts.items()
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        }
    row = raw.get("row")
    if isinstance(row, Mapping):
        cells = row.get("cells")
        data["row"] = {
            "text": masker.clip(row.get("text"), 500) or "",
            "cells": [
                masker.clip(cell, 200) or ""
                for cell in (cells if isinstance(cells, list) else [])[:20]
            ],
            "key": masker.clip(row.get("key"), 200),
            "role_name_count": row.get("role_name_count")
            if isinstance(row.get("role_name_count"), int)
            else None,
            "text_count": row.get("text_count") if isinstance(row.get("text_count"), int) else None,
        }
    try:
        return SnapshotElement.model_validate(data)
    except ValidationError:
        return None


async def _capture(
    page: Page,
    *,
    step_id: str,
    step_kind: str,
    error_code: str,
    masker: Masker,
    max_elements: int,
) -> PageSnapshot | None:
    frames, truncated = await _frames(page, masker)
    config = {
        "roles": sorted(snapshot_roles(step_kind)),
        "testIds": step_kind in _TEXT_KINDS,
        "max": max_elements,
    }
    snapshot_frames: list[SnapshotFrame] = []
    elements: list[SnapshotElement] = []
    title = ""
    for position, (frame, path) in enumerate(frames):
        remaining = max_elements - len(elements)
        if remaining <= 0:
            truncated = True
            break
        try:
            raw = await frame.evaluate(_SNAPSHOT_JS, {**config, "max": remaining})
        except PlaywrightError:
            truncated = True
            if position == 0:
                return None
            continue
        if not isinstance(raw, Mapping):
            continue
        frame_index = len(snapshot_frames)
        snapshot_frames.append(
            SnapshotFrame(
                path=path, url=masker.clip(page_path(_clip(raw.get("url"), 2000)), 500) or ""
            )
        )
        if position == 0:
            title = masker.clip(raw.get("title"), 300) or ""
        if raw.get("truncated"):
            truncated = True
        items = raw.get("elements")
        for item in items if isinstance(items, list) else []:
            element = _element(item, frame_index, masker)
            if element is not None:
                elements.append(element)
            if len(elements) >= max_elements:
                break
            # Deixa o laço de eventos andar (o prazo e os sinais do executor) entre um
            # elemento e outro.
            await asyncio.sleep(0)
    if not snapshot_frames:
        return None
    try:
        snapshot = PageSnapshot(
            step_id=step_id,
            step_kind=step_kind,
            error_code=error_code,
            url=masker.clip(page_path(page.url), 500) or "",
            title=title[:300],
            frames=snapshot_frames,
            elements=elements,
            truncated=truncated,
        )
    except ValidationError:
        return None
    return fit(snapshot)


def fit(snapshot: PageSnapshot, max_bytes: int = _TARGET_BYTES) -> PageSnapshot:
    """O retrato dentro do limite: grande demais, os últimos elementos ficam de fora."""
    while len(snapshot.model_dump_json()) > max_bytes and snapshot.elements:
        keep = len(snapshot.elements) * 3 // 4
        snapshot = snapshot.model_copy(
            update={"elements": snapshot.elements[:keep], "truncated": True}
        )
    return snapshot


async def capture(
    page: Page,
    *,
    step_id: str,
    step_kind: str,
    error_code: str,
    mask_values: Iterable[str] = (),
    max_elements: int = MAX_SNAPSHOT_ELEMENTS,
    timeout_s: float = CAPTURE_TIMEOUT_S,
) -> PageSnapshot | None:
    """Retrato só de leitura dos elementos que podem ser o alvo do passo (ou ``None``)."""
    if page.is_closed():
        return None
    masker = Masker(mask_values, deadline=time.monotonic() + timeout_s)
    try:
        async with asyncio.timeout(timeout_s):
            return await _capture(
                page,
                step_id=step_id,
                step_kind=step_kind,
                error_code=error_code,
                masker=masker,
                max_elements=max(1, min(max_elements, MAX_SNAPSHOT_ELEMENTS)),
            )
    except Exception:
        # Melhor esforço (o prazo, a página no meio de uma navegação, um retrato fora do
        # contrato ou qualquer outro erro): nada do retrato muda o desfecho da execução.
        return None
