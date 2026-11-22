"""Abertura do Chromium e dos contextos de execução.

Cada execução recebe um contexto novo (cookies, armazenamento e downloads
isolados), que nunca é reaproveitado por outra execução.
"""

import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright

__all__ = [
    "DEFAULT_CHROMIUM",
    "LOCALE",
    "TIMEZONE",
    "VIDEO_SIZE",
    "VIEWPORT",
    "ClientCertificate",
    "launch_browser",
    "new_run_context",
    "open_browser",
    "resolve_executable",
    "run_context",
]

DEFAULT_CHROMIUM = "/usr/bin/chromium"
LOCALE = "pt-BR"
TIMEZONE = "America/Sao_Paulo"
VIEWPORT = {"width": 1280, "height": 800}
#: Tamanho do vídeo da execução: o mesmo da tela, nunca maior.
VIDEO_SIZE = {"width": VIEWPORT["width"], "height": VIEWPORT["height"]}

#: Certificado de cliente (A1) no formato do Playwright: ``origin``, ``pfxPath`` e ``passphrase``.
type ClientCertificate = dict[str, str]


def resolve_executable(explicit: str | None = None) -> str | None:
    """Caminho do Chromium: o informado, ``RAPA_CHROMIUM_PATH``, ``/usr/bin/chromium``
    ou ``None`` (o navegador padrão do Playwright)."""
    if explicit:
        return explicit
    configured = os.environ.get("RAPA_CHROMIUM_PATH")
    if configured:
        return configured
    if Path(DEFAULT_CHROMIUM).exists():
        return DEFAULT_CHROMIUM
    return None


async def launch_browser(
    playwright: Playwright,
    *,
    executable_path: str | None = None,
    args: Sequence[str] = (),
) -> Browser:
    """Abre o Chromium sem janela."""
    return await playwright.chromium.launch(
        executable_path=resolve_executable(executable_path),
        headless=True,
        args=list(args),
    )


async def new_run_context(
    browser: Browser,
    *,
    client_certificates: Sequence[ClientCertificate] = (),
    record_video_dir: Path | None = None,
) -> BrowserContext:
    """Contexto novo para uma única execução, com idioma e fuso do Brasil.

    Com ``record_video_dir``, cada página do contexto é filmada nessa pasta (no
    tamanho da tela); o vídeo fica completo quando o contexto fecha.
    """
    options: dict[str, Any] = {}
    if client_certificates:
        options["client_certificates"] = [dict(cert) for cert in client_certificates]
    if record_video_dir is not None:
        options["record_video_dir"] = str(record_video_dir)
        options["record_video_size"] = dict(VIDEO_SIZE)
    return await browser.new_context(
        accept_downloads=True,
        locale=LOCALE,
        timezone_id=TIMEZONE,
        viewport={"width": VIEWPORT["width"], "height": VIEWPORT["height"]},
        **options,
    )


@asynccontextmanager
async def open_browser(
    *, executable_path: str | None = None, args: Sequence[str] = ()
) -> AsyncIterator[Browser]:
    """Playwright e Chromium abertos até o fim do bloco ``async with``."""
    async with async_playwright() as playwright:
        browser = await launch_browser(playwright, executable_path=executable_path, args=args)
        try:
            yield browser
        finally:
            await browser.close()


@asynccontextmanager
async def run_context(
    browser: Browser, *, client_certificates: Sequence[ClientCertificate] = ()
) -> AsyncIterator[BrowserContext]:
    """Contexto de uma execução, fechado (com tudo o que guardou) no fim do bloco."""
    context = await new_run_context(browser, client_certificates=client_certificates)
    try:
        yield context
    finally:
        await context.close()
