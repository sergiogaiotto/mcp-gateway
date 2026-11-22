"""Estado compartilhado pelas páginas: fábrica de backend, login, histórico e opções."""

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from urllib.parse import quote
from uuid import UUID

from fastapi import Request
from fastapi.responses import RedirectResponse

from rapa.core.contracts import ContractError, Principal, VisibleNames
from rapa.ui.auth import LOGIN_PATH, UiAuth, UiSession, safe_return_path
from rapa.ui.backend import Backend, BackendError, BackendUnavailable, NotAuthenticated

__all__ = ["BackendFactory", "UiContext", "UiSettings", "Viewer"]

logger = logging.getLogger(__name__)

type BackendFactory = Callable[[UiSession], Backend]

_CHECK_FAILED = "não foi possível conferir sua sessão agora; tente de novo em instantes"


@dataclass(frozen=True)
class UiSettings:
    """Opções da interface."""

    title: str = "RaPA"
    #: Intervalo de atualização do estado de uma execução, em segundos.
    poll_interval_s: float = 2.0
    #: Tempo máximo para montar uma página (chamadas à API incluídas).
    page_timeout_s: float = 20.0
    #: Endereço base do websocket da visão ao vivo (``wss://api.exemplo``); sem ele, o
    #: navegador usa o mesmo endereço da interface (a API e a interface no mesmo domínio).
    live_ws_base: str | None = None
    #: Tamanho máximo da planilha de um lote, em bytes (o ``RAPA_LOTE_MAX_BYTES`` do
    #: servidor; o envio pela tela recusa antes um arquivo maior).
    batch_max_bytes: int = 5 * 1024 * 1024
    #: Nomes legíveis das áreas e empresas (só para exibição), para quando o backend não
    #: tem os nomes (a plataforma em memória do modo de desenvolvimento e dos testes). Os
    #: do backend (:meth:`Viewer.load_names`) valem antes destes.
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
        #: Os nomes da empresa e das áreas que a pessoa enxerga, lidos uma vez por página.
        self.names = VisibleNames()

    def __repr__(self) -> str:
        return f"Viewer(subject={self.principal.subject!r})"

    async def load_names(self) -> None:
        """Lê do backend o nome da empresa e os das áreas que a pessoa enxerga.

        As mesmas regras do resto (a empresa vem da sessão; as áreas, dos papéis): nunca
        o nome de outra empresa. São só para mostrar: se não vierem (o serviço não
        respondeu), a página usa os nomes configurados ou o começo do identificador.
        """
        try:
            backend = await self.backend()
            self.names = await backend.visible_names()
        except NotAuthenticated:
            raise
        except (ContractError, BackendError) as exc:
            logger.warning("sem os nomes da empresa e das áreas: %s", type(exc).__name__)

    def area_label(self, area_id: UUID) -> str:
        """O nome da área (ou "Área" e o começo do identificador, se não houver)."""
        return self.names.area_names.get(area_id) or self.ctx.settings.area_label(area_id)

    def company_label(self) -> str:
        """O nome da empresa da pessoa."""
        return self.names.company_name or self.ctx.settings.company_label(self.principal.company_id)

    async def backend(self) -> Backend:
        """Backend no nome do principal atual da sessão, que precisa continuar válida.

        A sessão confere de novo a conta e os papéis (veja :meth:`UiAuth.session_by_key`):
        conta sem acesso encerra a sessão; papéis mudados passam a valer nesta ação,
        sem pedir login de novo. Se não der para conferir, a ação não segue.
        """
        try:
            session = await self.ctx.auth.fresh_session(self._session_key)
        except Exception as exc:
            logger.warning("falha ao conferir a sessão da interface: %s", type(exc).__name__)
            raise BackendUnavailable(_CHECK_FAILED) from None
        if session is None or not _same_person(session.principal, self.principal):
            raise NotAuthenticated("sua sessão expirou; entre de novo")
        self.principal = session.principal
        return self.ctx.backend_factory(session)


def _same_person(a: Principal, b: Principal) -> bool:
    return a.subject == b.subject and a.company_id == b.company_id


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
        """Quem está logado, ou None.

        Levanta :class:`BackendUnavailable` quando não dá para conferir a conta agora
        (banco fora do ar): a página nunca abre com o principal antigo da sessão.
        """
        try:
            session = await self.auth.session_from_request(request)
        except Exception as exc:
            logger.warning("falha ao conferir a sessão da interface: %s", type(exc).__name__)
            raise BackendUnavailable(_CHECK_FAILED) from None
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
