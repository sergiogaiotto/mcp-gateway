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
from collections.abc import Iterable, Mapping
from importlib.resources import files
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Frame, Page
from pydantic import ValidationError

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


class Masker:
    """Troca valores sigilosos por ``REMOVIDO`` em qualquer texto, sem distinguir caixa."""

    def __init__(self, values: Iterable[str]) -> None:
        unique = sorted({v for v in values if len(v) >= _MIN_MASKED}, key=len, reverse=True)
        self._pattern = (
            re.compile("|".join(re.escape(v) for v in unique), re.IGNORECASE) if unique else None
        )

    def text(self, value: str) -> str:
        if self._pattern is None:
            return value
        return self._pattern.sub(REDACTED, value)

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Mapping):
            return {key: self.value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.value(item) for item in value]
        return value


def flatten_strings(value: Any, *, depth: int = 0) -> list[str]:
    """Todos os textos de uma entrada (também os de listas e dos itens delas)."""
    if depth > 8:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [s for item in value.values() for s in flatten_strings(item, depth=depth + 1)]
    if isinstance(value, (list, tuple)):
        return [s for item in value for s in flatten_strings(item, depth=depth + 1)]
    return []


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
    return f"{parts.scheme}://{host}{port}{parts.path}"[:500]


def _clip(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text[:limit] or None


async def _frame_info(frame: Frame) -> SnapshotFrameInfo | None:
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
            id=_clip(raw.get("id"), 300),
            name=_clip(raw.get("name"), 300),
            title=_clip(raw.get("title"), 300),
            src=(urlsplit(src)._replace(query="", fragment="").geturl()[:500] or None)
            if src
            else None,
        )
    except ValidationError, ValueError:
        return None


async def _frames(page: Page) -> tuple[list[tuple[Frame, list[SnapshotFrameInfo]]], bool]:
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
            info = await _frame_info(child)
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
        limit = 100 if key == "autocomplete" else 300
        value = _clip(raw.get(key), limit)
        data[key] = masker.text(value)[:limit] if value else None
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
            "text": masker.text(_clip(row.get("text"), 500) or ""),
            "cells": [
                masker.text(_clip(cell, 200) or "")[:300]
                for cell in (cells if isinstance(cells, list) else [])[:20]
            ],
            "key": masker.text(row_key)[:200] if (row_key := _clip(row.get("key"), 200)) else None,
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
    frames, truncated = await _frames(page)
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
        clean_path = [
            info.model_copy(
                update={
                    key: masker.text(text)[:500] if (text := getattr(info, key)) else None
                    for key in ("id", "name", "title", "src")
                }
            )
            for info in path
        ]
        snapshot_frames.append(
            SnapshotFrame(
                path=clean_path, url=masker.text(page_path(_clip(raw.get("url"), 2000)))[:500]
            )
        )
        if position == 0:
            title = masker.text(_clip(raw.get("title"), 300) or "")
        if raw.get("truncated"):
            truncated = True
        items = raw.get("elements")
        for item in items if isinstance(items, list) else []:
            element = _element(item, frame_index, masker)
            if element is not None:
                elements.append(element)
            if len(elements) >= max_elements:
                break
    if not snapshot_frames:
        return None
    try:
        snapshot = PageSnapshot(
            step_id=step_id,
            step_kind=step_kind,
            error_code=error_code,
            url=masker.text(page_path(page.url)),
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
    masker = Masker(mask_values)
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
