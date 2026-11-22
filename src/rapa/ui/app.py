"""Montagem da interface NiceGUI sobre uma aplicação FastAPI.

Uso::

    app = FastAPI()
    create_ui(app, backend_factory=RestBackendFactory("https://api.rapa"), auth=UiAuth(oidc=...))

As rotas ``/auth/*`` (login, retorno do OIDC, saída) entram na própria
aplicação FastAPI, porque precisam gravar cookies, assim como os arquivos das
evidências (``/execucoes/{id}/artefatos/{nome}``); as páginas entram no NiceGUI,
montado em ``/``. Chame ``create_ui`` depois de registrar as outras rotas da
aplicação: a montagem em ``/`` atende tudo o que vier depois.

O NiceGUI guarda as páginas num estado global do processo, então
``create_ui`` só pode ser chamado uma vez por processo (os testes reiniciam o
estado do NiceGUI entre um caso e outro).
"""

from fastapi import FastAPI
from nicegui import app as nicegui_app
from nicegui import ui
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from rapa.ui.auth import HOME_PATH, UiAuth
from rapa.ui.context import BackendFactory, UiContext, UiSettings
from rapa.ui.evidence import evidence_router
from rapa.ui.pages import register_pages
from rapa.ui.recording import STATIC_DIR, STATIC_PATH

__all__ = ["SecurityHeadersMiddleware", "create_ui"]


class SecurityHeadersMiddleware:
    """Cabeçalhos de proteção nas respostas da interface (sem moldura, sem farejar tipo)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("Referrer-Policy", "same-origin")
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _not_found() -> None:
    """Página para endereços que não existem (o NiceGUI usa a página raiz como reserva)."""
    ui.status_code(404)
    with ui.column().classes("w-full items-center p-12 gap-4").mark("pagina-404"):
        ui.label("Página não encontrada").classes("text-h5")
        ui.label("O endereço não existe ou mudou.")
        ui.link("Ir para o catálogo", HOME_PATH)


def _page_error() -> None:
    """Erro inesperado ao montar uma página: mensagem genérica, sem detalhes da exceção."""
    with ui.column().classes("w-full items-center p-12 gap-4").mark("pagina-erro"):
        ui.label("Algo deu errado ao montar esta página").classes("text-h5")
        ui.label("Tente de novo em instantes. Se continuar, avise o time da plataforma.")
        ui.link("Ir para o catálogo", HOME_PATH)


def create_ui(
    app: FastAPI,
    *,
    backend_factory: BackendFactory,
    auth: UiAuth,
    settings: UiSettings | None = None,
) -> None:
    """Registra as rotas de login e as páginas, e monta o NiceGUI em ``app``."""
    settings = settings or UiSettings()
    ctx = UiContext(backend_factory, auth, settings)
    app.include_router(auth.router())
    # Os arquivos das evidências (telas, vídeo, downloads), pela sessão do cookie.
    app.include_router(evidence_router(ctx))
    register_pages(ctx)
    # Só o JavaScript que o time mantém (a visão ao vivo); nada com dados.
    nicegui_app.add_static_files(STATIC_PATH, STATIC_DIR, max_cache_age=300)
    nicegui_app.on_page_exception(_page_error)
    nicegui_app.add_middleware(SecurityHeadersMiddleware)
    ui.run_with(
        app,
        root=_not_found,
        title=settings.title,
        language="pt-BR",
        dark=False,
        show_welcome_message=False,
        # Sem storage_secret: a autoridade vem só da sessão no servidor, nunca do app.storage.
        storage_secret=None,
    )
