"""Estado compartilhado pelas páginas: fábrica de backend, login, histórico e opções."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from urllib.parse import quote
from uuid import UUID

from fastapi import Request
from fastapi.responses import RedirectResponse

from rapa.core.contracts import Principal
from rapa.ui.auth import LOGIN_PATH, UiAuth, UiSession, safe_return_path
from rapa.ui.backend import Backend, NotAuthenticated

__all__ = ["BackendFactory", "UiContext", "UiSettings", "Viewer"]

type BackendFactory = Callable[[UiSession], Backend]


@dataclass(frozen=True)
class UiSettings:
    """Opções da interface."""

    title: str = "RaPA"
    #: Intervalo de atualização do estado de uma execução, em segundos.
    poll_interval_s: float = 2.0
    #: Tempo máximo para montar uma página (chamadas à API incluídas).
    page_timeout_s: float = 20.0
    #: Nomes legíveis das áreas e empresas (só para exibição).
    area_labels: Mapping[UUID, str] = field(default_factory=dict)
    company_labels: Mapping[UUID, str] = field(default_factory=dict)

    def area_label(self, area_id: UUID) -> str:
        return self.area_labels.get(area_id, f"Área {str(area_id)[:8]}")

    def company_label(self, company_id: UUID) -> str:
        return self.company_labels.get(company_id, f"Empresa {str(company_id)[:8]}")


class Viewer:
    """Quem está vendo a página. Toda ação confere a sessão de novo no servidor."""

    def __init__(self, ctx: UiContext, session: UiSession) -> None:
        self.ctx = ctx
        self._session_key = session.key
        self.principal: Principal = session.principal
        self.display_name = session.display_name

    def __repr__(self) -> str:
        return f"Viewer(subject={self.principal.subject!r})"

    async def backend(self) -> Backend:
        """Backend no nome do principal da sessão, que precisa continuar válida."""
        session = await self.ctx.auth.fresh_session(self._session_key)
        if session is None or session.principal != self.principal:
            raise NotAuthenticated("sua sessão expirou; entre de novo")
        return self.ctx.backend_factory(session)


class UiContext:
    def __init__(
        self,
        backend_factory: BackendFactory,
        auth: UiAuth,
        settings: UiSettings,
    ) -> None:
        self.backend_factory = backend_factory
        self.auth = auth
        self.settings = settings

    async def viewer(self, request: Request) -> Viewer | None:
        session = await self.auth.session_from_request(request)
        return Viewer(self, session) if session is not None else None

    @staticmethod
    def login_redirect(request: Request, *, expired: bool = False) -> RedirectResponse:
        """Manda para o login, guardando o caminho atual para voltar depois."""
        current = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        target = f"{LOGIN_PATH}?volta={quote(safe_return_path(current), safe='')}"
        if expired:
            target += "&erro=sessao_expirada"
        response = RedirectResponse(target, status_code=303)
        response.headers["Cache-Control"] = "no-store"
        return response
