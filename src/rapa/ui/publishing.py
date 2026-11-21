"""Telas de publicação: Agendas, Webhooks e Lotes (lote por planilha).

A interface continua fina: quem pode, o cálculo da próxima ocorrência, a
assinatura dos webhooks e a conferência das planilhas ficam no plano de controle;
estas telas só usam o :class:`rapa.ui.backend.Backend`. Os botões de criar e
mudar só aparecem para quem opera (ou administra) a área do robô
(:func:`rapa.core.contracts.can_automate`), e o servidor confere de novo.

- **Agendas.** Escolher um robô publicado, a expressão cron (com atalhos), o fuso e
  as entradas fixas (o mesmo formulário de Executar). A tela mostra a próxima
  ocorrência e, na agenda, as ocorrências com o que aconteceu com cada uma.
- **Webhooks.** A chave de assinatura aparece uma vez, logo depois de criar ou de
  trocar a chave, com o jeito de assinar; depois disso, ninguém a vê de novo.
- **Lotes.** Enviar a planilha (xlsx ou CSV) mostra a prévia: as colunas e as
  entradas, as primeiras linhas convertidas e os erros por linha. Só uma planilha
  sem erros pode virar lote ("Criar lote"). O lote mostra o andamento e baixa o
  resultado em CSV ou xlsx.
"""

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from nicegui import ui
from nicegui.events import UploadEventArguments

from rapa.core.contracts import (
    BatchFormat,
    BatchInfo,
    BatchPreview,
    ContractError,
    InvalidInput,
    RobotSummary,
    RunState,
    ScheduleChanges,
    ScheduleOccurrenceStatus,
    ScheduleSpec,
    WebhookChanges,
    WebhookSecret,
    WebhookSpec,
    can_automate,
)
from rapa.core.cron import DEFAULT_TIMEZONE
from rapa.core.ids import new_id
from rapa.ui.backend import BackendError, BackendUnavailable, NotAuthenticated
from rapa.ui.context import Viewer
from rapa.ui.forms import SchemaForm
from rapa.ui.labels import (
    OCCURRENCE_REASON_LABELS,
    RUN_STATE_LABELS,
    format_datetime,
)
from rapa.ui.layout import notify_error, run_action, simple_table, state_badge
from rapa.ui.permissions import can_automate_anywhere

__all__ = [
    "CRON_PRESETS",
    "batch_page",
    "batches_page",
    "schedule_page",
    "schedules_page",
    "webhooks_page",
]

#: Atalhos de expressão cron oferecidos na tela.
CRON_PRESETS: dict[str, str] = {
    "0 9 * * 1-5": "Dias úteis às 9h",
    "0 8 * * *": "Todo dia às 8h",
    "0 * * * *": "De hora em hora",
    "*/15 * * * *": "A cada 15 minutos",
    "0 7 1 * *": "Dia 1 de cada mês às 7h",
}
_BATCHES_SHOWN = 50
_ROWS_SHOWN = 500
_SAMPLE_SHOWN = 5


def _parse_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


def _not_found(text: str) -> None:
    with ui.card().classes("w-full").mark("nao-encontrado"):
        ui.label(text)


async def _automatable(viewer: Viewer) -> list[RobotSummary]:
    backend = await viewer.backend()
    return [
        summary
        for summary in await backend.list_catalog()
        if can_automate(viewer.principal, summary.area_id)
    ]


def _no_permission(marker: str, what: str) -> None:
    with ui.card().classes("w-full").mark(marker):
        ui.label(f"Você pode ver {what}, mas não criar nem mudar.")
        ui.label(
            "Só quem opera robôs (ou administra) a área pode. Peça o papel ao "
            "administrador da sua empresa."
        ).classes("text-caption")


# ---------------------------------------------------------------------------
# Agendas
# ---------------------------------------------------------------------------


async def schedules_page(viewer: Viewer) -> None:
    """As agendas dos robôs das áreas da pessoa e o formulário de uma nova."""
    ui.label("Agendas").classes("text-h5")
    ui.label(
        "Um robô publicado roda sozinho nos horários da expressão cron, sempre na versão "
        "publicada naquele momento. Se o serviço estiver fora do ar no horário, a ocorrência "
        "ainda roda por alguns minutos; depois disso, fica registrada como perdida."
    ).classes("text-caption")
    backend = await viewer.backend()
    schedules = await backend.list_schedules()
    with ui.card().classes("w-full").mark("lista-agendas"):
        if not schedules:
            ui.label("Nenhuma agenda ainda.").mark("sem-agendas")
        for item in schedules:
            with (
                ui.row()
                .classes("w-full items-center justify-between")
                .mark(f"agenda-{item.schedule_id}")
            ):
                with ui.column().classes("gap-0"):
                    ui.label(item.title).classes("text-weight-medium")
                    when = (
                        f"próxima em {format_datetime(item.next_run_at)}"
                        if item.enabled
                        else "desligada"
                    )
                    ui.label(f"{item.robot_slug} · {item.cron} ({item.timezone}) · {when}").classes(
                        "text-caption"
                    )
                ui.button(
                    "Abrir",
                    on_click=lambda s=item.schedule_id: ui.navigate.to(f"/agendas/{s}"),
                ).props("flat").mark(f"abrir-agenda-{item.schedule_id}")
    if not can_automate_anywhere(viewer.principal):
        _no_permission("sem-permissao-agendas", "as agendas")
        return
    robots = await _automatable(viewer)
    _new_schedule_card(viewer, robots)


def _new_schedule_card(viewer: Viewer, robots: list[RobotSummary]) -> None:
    with ui.card().classes("w-full").mark("nova-agenda"):
        ui.label("Nova agenda").classes("text-h6")
        if not robots:
            ui.label("Nenhum robô publicado nas áreas que você opera.").mark("sem-robos")
            return
        by_id = {str(r.robot_id): r for r in robots}
        robot_select = (
            ui.select({k: r.title for k, r in by_id.items()}, label="Robô", value=next(iter(by_id)))
            .classes("w-96")
            .mark("robo-agenda")
        )
        title = ui.input("Nome da agenda").classes("w-96").mark("titulo-agenda")
        with ui.row().classes("items-end gap-2"):
            cron = (
                ui.input("Expressão cron (minuto hora dia mês dia-da-semana)", value="0 9 * * 1-5")
                .classes("w-96")
                .mark("cron-agenda")
            )
            ui.select(
                CRON_PRESETS,
                label="Atalhos",
                on_change=lambda e: cron.set_value(e.value) if e.value else None,
            ).classes("w-64").mark("atalho-cron")
        zone = ui.input("Fuso horário", value=DEFAULT_TIMEZONE).classes("w-96").mark("fuso-agenda")
        ui.label("Entradas fixas de cada execução").classes("text-subtitle1")
        form_box = ui.column().classes("w-full")
        state: dict[str, Any] = {}

        def build_form() -> None:
            form_box.clear()
            summary = by_id.get(robot_select.value or "")
            if summary is None:
                return
            with form_box:
                form = SchemaForm(summary.input_schema)
                form.build()
            state["form"] = form

        robot_select.on_value_change(lambda _: build_form())
        build_form()

        async def create() -> None:
            summary = by_id.get(robot_select.value or "")
            form: SchemaForm | None = state.get("form")
            if summary is None or form is None:
                ui.notify("Escolha um robô.", type="warning")
                return
            values = form.submit()
            if values is None:
                ui.notify("Corrija os campos destacados.", type="warning")
                return
            backend = await viewer.backend()
            try:
                info = await backend.create_schedule(
                    ScheduleSpec(
                        robot_id=summary.robot_id,
                        title=(title.value or "").strip() or summary.title,
                        cron=(cron.value or "").strip(),
                        timezone=(zone.value or "").strip() or DEFAULT_TIMEZONE,
                        inputs=values,
                    )
                )
            except InvalidInput as exc:
                form.show_server_errors(exc.errors)
                notify_error(exc)
                return
            ui.notify("Agenda criada.", type="positive")
            ui.navigate.to(f"/agendas/{info.schedule_id}")

        ui.button("Criar agenda", on_click=lambda: run_action(create)).mark("criar-agenda")


async def schedule_page(viewer: Viewer, schedule_id: str) -> None:
    """Uma agenda: o que ela roda, quando, e o que aconteceu com cada ocorrência."""
    parsed = _parse_uuid(schedule_id)
    backend = await viewer.backend()
    info = await backend.get_schedule(parsed) if parsed is not None else None
    if info is None or parsed is None:
        _not_found("Agenda não encontrada.")
        return
    occurrences = await backend.list_occurrences(parsed, 50)
    ui.label(info.title).classes("text-h5").mark("titulo-agenda")
    with ui.card().classes("w-full").mark("detalhes-agenda"):
        rows = [
            ["Robô", info.robot_slug],
            ["Expressão", info.cron],
            ["Fuso", info.timezone],
            ["Situação", "Ligada" if info.enabled else "Desligada"],
            ["Próxima", format_datetime(info.next_run_at) if info.enabled else "—"],
            ["Última ocorrência", format_datetime(info.last_occurrence_at)],
            ["Criada por", info.created_by],
        ]
        rows += [[f"Entrada: {name}", str(value)] for name, value in info.inputs.items()]
        simple_table(["", ""], rows)
        if can_automate(viewer.principal, info.area_id):

            async def toggle() -> None:
                current = await viewer.backend()
                await current.update_schedule(parsed, ScheduleChanges(enabled=not info.enabled))
                ui.notify(
                    "Agenda desligada." if info.enabled else "Agenda ligada.", type="positive"
                )
                ui.navigate.to(f"/agendas/{parsed}")

            async def remove() -> None:
                current = await viewer.backend()
                await current.delete_schedule(parsed)
                ui.notify("Agenda removida.", type="positive")
                ui.navigate.to("/agendas")

            with ui.row().classes("gap-2"):
                ui.button(
                    "Desligar" if info.enabled else "Ligar",
                    on_click=lambda: run_action(toggle),
                ).mark("desligar-agenda" if info.enabled else "ligar-agenda")
                ui.button("Remover", color="negative", on_click=lambda: run_action(remove)).props(
                    "flat"
                ).mark("remover-agenda")
    with ui.card().classes("w-full").mark("ocorrencias"):
        ui.label("Ocorrências").classes("text-h6")
        if not occurrences:
            ui.label("Nenhuma ocorrência ainda.").mark("sem-ocorrencias")
        for occurrence in occurrences:
            with ui.row().classes("w-full items-center gap-2").mark("ocorrencia"):
                ui.label(format_datetime(occurrence.scheduled_for)).classes("text-weight-medium")
                if occurrence.status is ScheduleOccurrenceStatus.ENQUEUED and occurrence.run_id:
                    ui.label("virou execução")
                    ui.link("ver execução", f"/execucoes/{occurrence.run_id}")
                else:
                    reason = occurrence.reason or ""
                    ui.label(OCCURRENCE_REASON_LABELS.get(reason, reason)).classes(
                        "text-warning"
                    ).mark(f"motivo-{reason}")


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------


def _show_secret(box: ui.column, created: WebhookSecret) -> None:
    hook = created.webhook
    box.clear()
    with box, ui.card().classes("w-full bg-amber-1").mark("chave-webhook"):
        ui.label("Chave de assinatura").classes("text-h6")
        ui.label(
            "Copie a chave agora: ela não aparece de novo. Se perder, troque a chave."
        ).classes("text-warning text-weight-medium")
        ui.input("Chave", value=created.secret).props("readonly").classes("w-full").mark(
            "valor-chave"
        )
        ui.label(f"Endereço: POST {hook.path}").mark("endereco-webhook")
        ui.label(
            "O corpo é um objeto JSON com as entradas do robô. Mande os cabeçalhos "
            "X-RaPA-Timestamp (segundos desde 1970), X-RaPA-Delivery (identificador único da "
            "entrega) e X-RaPA-Signature (sha256= e o HMAC-SHA256, com a chave, de "
            "<timestamp>.<delivery>.<corpo>). A mesma entrega de novo devolve a mesma execução."
        ).classes("text-caption")


async def webhooks_page(viewer: Viewer) -> None:
    """Os webhooks dos robôs publicados e o formulário de um novo."""
    ui.label("Webhooks").classes("text-h5")
    ui.label(
        "Outro sistema chama o endereço do webhook, sem token, assinando cada entrega com a "
        "chave. Cada entrega vira uma execução do robô publicado."
    ).classes("text-caption")
    backend = await viewer.backend()
    robots = await backend.list_catalog()
    secret_box = ui.column().classes("w-full")
    with ui.column().classes("w-full gap-3").mark("lista-webhooks"):
        total = 0
        for summary in robots:
            hooks = await backend.list_webhooks(summary.robot_id)
            if not hooks:
                continue
            total += len(hooks)
            manage = can_automate(viewer.principal, summary.area_id)
            with ui.card().classes("w-full").mark(f"webhooks-{summary.slug}"):
                ui.label(summary.title).classes("text-h6")
                for hook in hooks:
                    _webhook_row(viewer, summary, hook.webhook_id, hook, manage, secret_box)
        if not total:
            ui.label("Nenhum webhook ainda.").mark("sem-webhooks")
    if not can_automate_anywhere(viewer.principal):
        _no_permission("sem-permissao-webhooks", "os webhooks")
        return
    automatable = [r for r in robots if can_automate(viewer.principal, r.area_id)]
    with ui.card().classes("w-full").mark("novo-webhook"):
        ui.label("Novo webhook").classes("text-h6")
        if not automatable:
            ui.label("Nenhum robô publicado nas áreas que você opera.").mark("sem-robos")
            return
        by_id = {str(r.robot_id): r for r in automatable}
        robot_select = (
            ui.select({k: r.title for k, r in by_id.items()}, label="Robô", value=next(iter(by_id)))
            .classes("w-96")
            .mark("robo-webhook")
        )
        title = ui.input("Nome do webhook").classes("w-96").mark("titulo-webhook")
        rate = (
            ui.number("Entregas por minuto", value=60, min=1, max=6000, precision=0)
            .classes("w-64")
            .mark("por-minuto-webhook")
        )

        async def create() -> None:
            summary = by_id.get(robot_select.value or "")
            if summary is None:
                ui.notify("Escolha um robô.", type="warning")
                return
            current = await viewer.backend()
            created = await current.create_webhook(
                summary.robot_id,
                WebhookSpec(
                    title=(title.value or "").strip() or summary.title,
                    rate_per_minute=int(rate.value or 60),
                ),
            )
            ui.notify("Webhook criado. Copie a chave agora.", type="positive")
            _show_secret(secret_box, created)

        ui.button("Criar webhook", on_click=lambda: run_action(create)).mark("criar-webhook")


def _webhook_row(
    viewer: Viewer,
    summary: RobotSummary,
    webhook_id: UUID,
    hook: Any,
    manage: bool,
    secret_box: ui.column,
) -> None:
    with ui.row().classes("w-full items-center justify-between").mark(f"webhook-{webhook_id}"):
        with ui.column().classes("gap-0"):
            ui.label(hook.title).classes("text-weight-medium")
            situation = "ligado" if hook.enabled else "desligado"
            last = format_datetime(hook.last_delivery_at)
            ui.label(
                f"{situation} · até {hook.rate_per_minute} por minuto · POST {hook.path} · "
                f"última entrega: {last}"
            ).classes("text-caption")
        if not manage:
            return

        async def rotate() -> None:
            current = await viewer.backend()
            created = await current.rotate_webhook(summary.robot_id, webhook_id)
            ui.notify("Chave trocada; a anterior deixou de valer.", type="positive")
            _show_secret(secret_box, created)

        async def toggle() -> None:
            current = await viewer.backend()
            await current.update_webhook(
                summary.robot_id, webhook_id, WebhookChanges(enabled=not hook.enabled)
            )
            ui.navigate.to("/webhooks")

        async def remove() -> None:
            current = await viewer.backend()
            await current.delete_webhook(summary.robot_id, webhook_id)
            ui.notify("Webhook removido.", type="positive")
            ui.navigate.to("/webhooks")

        with ui.row().classes("gap-1"):
            ui.button("Trocar chave", on_click=lambda: run_action(rotate)).props("flat").mark(
                f"trocar-chave-{webhook_id}"
            )
            ui.button(
                "Desligar" if hook.enabled else "Ligar", on_click=lambda: run_action(toggle)
            ).props("flat").mark(f"ligar-webhook-{webhook_id}")
            ui.button("Remover", color="negative", on_click=lambda: run_action(remove)).props(
                "flat"
            ).mark(f"remover-webhook-{webhook_id}")


# ---------------------------------------------------------------------------
# Lotes
# ---------------------------------------------------------------------------


def _progress_text(info: BatchInfo) -> str:
    parts = [
        f"{RUN_STATE_LABELS[state]}: {count}"
        for state, count in sorted(info.counts.items(), key=lambda item: item[0].value)
    ]
    return f"{info.done} de {info.total} terminadas" + (f" ({', '.join(parts)})" if parts else "")


async def batches_page(viewer: Viewer) -> None:
    """Os lotes das áreas da pessoa e o envio de uma planilha."""
    ui.label("Lotes").classes("text-h5")
    ui.label(
        "Cada linha da planilha vira uma execução do robô, com a menor prioridade da fila. "
        "Antes de criar, todas as linhas são conferidas: com um erro, nada é criado."
    ).classes("text-caption")
    backend = await viewer.backend()
    batches = await backend.list_batches(_BATCHES_SHOWN)
    with ui.card().classes("w-full").mark("lista-lotes"):
        if not batches:
            ui.label("Nenhum lote ainda.").mark("sem-lotes")
        for info in batches:
            with (
                ui.row()
                .classes("w-full items-center justify-between")
                .mark(f"lote-{info.batch_id}")
            ):
                with ui.column().classes("gap-0"):
                    ui.label(f"{info.file_name} · {info.robot_slug}").classes("text-weight-medium")
                    ui.label(
                        f"{_progress_text(info)} · {format_datetime(info.created_at)}"
                    ).classes("text-caption")
                ui.button(
                    "Abrir", on_click=lambda b=info.batch_id: ui.navigate.to(f"/lotes/{b}")
                ).props("flat").mark(f"abrir-lote-{info.batch_id}")
    if not can_automate_anywhere(viewer.principal):
        _no_permission("sem-permissao-lotes", "os lotes")
        return
    robots = await _automatable(viewer)
    NewBatch(viewer, robots).build()


class NewBatch:
    """Envio da planilha, prévia e confirmação de um lote novo."""

    def __init__(self, viewer: Viewer, robots: Sequence[RobotSummary]) -> None:
        self.viewer = viewer
        self.by_id = {str(r.robot_id): r for r in robots}
        self.file: tuple[str, bytes] | None = None
        self.preview_box: ui.column | None = None
        self.robot_select: ui.select | None = None
        #: Mesma chave enquanto a planilha não muda: um clique duplo não cria dois lotes.
        self.key = new_id().hex

    def build(self) -> None:
        with ui.card().classes("w-full").mark("novo-lote"):
            ui.label("Novo lote").classes("text-h6")
            if not self.by_id:
                ui.label("Nenhum robô publicado nas áreas que você opera.").mark("sem-robos")
                return
            ui.label(
                "Envie uma planilha .xlsx (só com valores, sem fórmulas) ou .csv. A primeira "
                "linha tem os títulos das colunas: o nome técnico ou o título de cada entrada."
            ).classes("text-caption")
            self.robot_select = (
                ui.select(
                    {k: r.title for k, r in self.by_id.items()},
                    label="Robô",
                    value=next(iter(self.by_id)),
                    on_change=lambda _: self._reset(),
                )
                .classes("w-96")
                .mark("robo-lote")
            )
            ui.upload(
                label="Planilha (.xlsx ou .csv)",
                auto_upload=True,
                max_files=1,
                on_upload=self._uploaded,
            ).props('accept=".xlsx,.csv"').classes("w-96").mark("arquivo-lote")
            self.preview_box = ui.column().classes("w-full").mark("area-previa")

    def _robot(self) -> RobotSummary | None:
        if self.robot_select is None:
            return None
        return self.by_id.get(self.robot_select.value or "")

    def _reset(self) -> None:
        self.file = None
        self.key = new_id().hex
        if self.preview_box is not None:
            self.preview_box.clear()

    async def _uploaded(self, event: UploadEventArguments) -> None:
        self.file = (event.file.name, await event.file.read())
        self.key = new_id().hex
        await run_action(self.preview)

    async def preview(self) -> None:
        robot = self._robot()
        if robot is None or self.file is None or self.preview_box is None:
            return
        backend = await self.viewer.backend()
        self.preview_box.clear()
        try:
            preview = await backend.preview_batch(robot.robot_id, self.file[0], self.file[1])
        except InvalidInput as exc:
            with self.preview_box, ui.card().classes("w-full bg-red-1").mark("erros-planilha"):
                ui.label(exc.message[:1].upper() + exc.message[1:]).classes("text-negative")
                for detail in exc.errors:
                    ui.label(detail).classes("text-negative text-caption")
            return
        with self.preview_box:
            self._render(preview)

    def _render(self, preview: BatchPreview) -> None:
        with ui.card().classes("w-full").mark("previa-lote"):
            ui.label(f"Prévia: {preview.file_name}").classes("text-h6")
            ui.label(
                f"{preview.rows} linhas para o robô {preview.robot_slug} "
                f"(versão {preview.version_no})."
            ).mark("linhas-previa")
            simple_table(
                ["Coluna", "Entrada do robô"],
                [[c.header, c.input or "ignorada"] for c in preview.columns],
                marker="colunas-previa",
            )
            if preview.file_errors:
                with ui.column().classes("w-full bg-red-1 p-2 rounded").mark("erros-planilha"):
                    for message in preview.file_errors:
                        ui.label(message).classes("text-negative")
            if preview.error_rows:
                with ui.column().classes("w-full bg-red-1 p-2 rounded").mark("erros-linhas"):
                    ui.label(
                        f"{preview.error_rows} linhas com erro; corrija a planilha e envie de novo."
                    ).classes("text-negative text-weight-medium")
                    for error in preview.errors:
                        ui.label(f"Linha {error.line}: {'; '.join(error.errors)}").classes(
                            "text-negative text-caption"
                        )
            if preview.sample:
                headers = list(dict.fromkeys(k for row in preview.sample for k in row))
                ui.label("Primeiras linhas, como o robô vai recebê-las").classes("text-subtitle1")
                simple_table(
                    headers,
                    [
                        [str(row.get(h, "")) for h in headers]
                        for row in preview.sample[:_SAMPLE_SHOWN]
                    ],
                    marker="amostra-previa",
                )
            if preview.ok:
                ui.button(
                    f"Criar lote com {preview.rows} execuções",
                    on_click=lambda: run_action(self.create),
                ).mark("confirmar-lote")

    async def create(self) -> None:
        robot = self._robot()
        if robot is None or self.file is None:
            return
        backend = await self.viewer.backend()
        try:
            info = await backend.create_batch(
                robot.robot_id, self.file[0], self.file[1], idempotency_key=self.key
            )
        except BackendUnavailable as exc:
            notify_error(exc)  # mesma chave: tentar de novo não duplica o lote
            return
        ui.notify(f"Lote criado com {info.total} execuções.", type="positive")
        ui.navigate.to(f"/lotes/{info.batch_id}")


async def batch_page(viewer: Viewer, batch_id: str) -> None:
    """Um lote: o andamento, as linhas e o resultado para baixar."""
    parsed = _parse_uuid(batch_id)
    backend = await viewer.backend()
    info = await backend.get_batch(parsed) if parsed is not None else None
    if info is None or parsed is None:
        _not_found("Lote não encontrado.")
        return
    ui.label(f"Lote: {info.file_name}").classes("text-h5").mark("titulo-lote")
    ui.label(
        f"Robô {info.robot_slug}, versão {info.version_no} · criado por {info.created_by} em "
        f"{format_datetime(info.created_at)}"
    ).classes("text-caption")
    BatchPanel(viewer, parsed, info).build()


class BatchPanel:
    """Andamento e linhas de um lote, atualizados até todas as execuções terminarem."""

    def __init__(self, viewer: Viewer, batch_id: UUID, info: BatchInfo) -> None:
        self.viewer = viewer
        self.batch_id = batch_id
        self.info = info
        self.box: ui.column | None = None
        self.timer: ui.timer | None = None

    def build(self) -> None:
        with ui.row().classes("gap-2"):
            for fmt, label in ((BatchFormat.CSV, "Baixar CSV"), (BatchFormat.XLSX, "Baixar xlsx")):
                ui.button(label, on_click=lambda f=fmt: run_action(lambda: self.download(f))).mark(
                    f"baixar-{fmt.value}"
                )
        self.box = ui.column().classes("w-full gap-3")
        self.timer = ui.timer(self.viewer.ctx.settings.poll_interval_s, self.refresh)
        self.timer.active = False
        ui.timer(0.01, self.refresh, once=True)

    async def download(self, fmt: BatchFormat) -> None:
        backend = await self.viewer.backend()
        file = await backend.batch_results(self.batch_id, fmt)
        ui.download.content(file.content, file.file_name, file.media_type)

    async def refresh(self) -> None:
        try:
            backend = await self.viewer.backend()
            info = await backend.get_batch(self.batch_id)
            rows = await backend.list_batch_rows(self.batch_id)
        except NotAuthenticated:
            self._stop()
            return
        except ContractError, BackendError:
            return
        if info is None or self.box is None:
            self._stop()
            return
        self.info = info
        self.box.clear()
        with self.box:
            with ui.card().classes("w-full").mark("andamento-lote"):
                ui.label("Andamento").classes("text-h6")
                ui.linear_progress(
                    value=info.done / info.total if info.total else 1.0, show_value=False
                )
                ui.label(_progress_text(info)).mark("texto-andamento")
                if info.finished:
                    ui.label("Todas as execuções do lote terminaram.").mark("lote-terminado")
            with ui.card().classes("w-full").mark("linhas-lote"):
                ui.label("Linhas").classes("text-h6")
                for row in rows[:_ROWS_SHOWN]:
                    with (
                        ui.row().classes("w-full items-center gap-2").mark(f"linha-lote-{row.line}")
                    ):
                        ui.label(f"Linha {row.line}").classes("text-weight-medium")
                        state_badge(row.state)
                        ui.link("execução", f"/execucoes/{row.run_id}")
                        if row.error_message or row.error_code:
                            ui.label(row.error_message or row.error_code or "").classes(
                                "text-negative text-caption"
                            )
                        elif row.state is RunState.SUCCEEDED and row.outputs:
                            ui.label(
                                ", ".join(f"{k}: {v}" for k, v in row.outputs.items())[:200]
                            ).classes("text-caption")
        if self.timer is not None:
            self.timer.active = not info.finished

    def _stop(self) -> None:
        if self.timer is not None:
            self.timer.active = False
