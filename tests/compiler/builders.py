"""Construção de gravações à mão para os testes do compilador (sem navegador)."""

from datetime import UTC, datetime
from typing import Any, cast

from pydantic import BaseModel

from rapa.recorder.models import (
    CheckEvent,
    ClickEvent,
    DownloadEvent,
    ElementDescriptor,
    FileEvent,
    FormInfo,
    FrameInfo,
    Heading,
    InputEvent,
    KeyEvent,
    LocatorCounts,
    NavigationCause,
    NavigationEvent,
    PageTextEvent,
    Recording,
    RecordingEvent,
    RequestEvent,
    RowInfo,
    SelectEvent,
)

BASE = "https://portal.exemplo.gov.br"


def _counts(**known: int | None) -> LocatorCounts:
    return LocatorCounts(**known)


def element(
    tag: str,
    *,
    role: str | None = None,
    name: str | None = None,
    label: str | None = None,
    text: str | None = None,
    input_type: str | None = None,
    id: str | None = None,
    name_attr: str | None = None,
    placeholder: str | None = None,
    test_id: str | None = None,
    css: str | None = None,
    frames: list[FrameInfo] | None = None,
    row: RowInfo | None = None,
    form: FormInfo | None = None,
    counts: dict[str, int | None] | None = None,
    **flags: Any,
) -> ElementDescriptor:
    """Descritor com contagens 1 para cada estratégia informada (a menos que dito o contrário)."""
    default_counts: dict[str, int | None] = {
        "role_name": 1 if role and name else None,
        "label": 1 if label else None,
        "placeholder": 1 if placeholder else None,
        "test_id": 1 if test_id else None,
        "text": 1 if text else None,
        "id": 1 if id else None,
        "name_attr": 1 if name_attr else None,
    }
    default_counts.update(counts or {})
    return ElementDescriptor(
        tag=tag,
        input_type=input_type,
        role=role,
        name=name,
        label=label,
        placeholder=placeholder,
        test_id=test_id,
        name_attr=name_attr,
        id=id,
        css=css or (f"#{id}" if id else f"body > {tag}"),
        text=text,
        frame_path=frames if frames is not None else [],
        row=row,
        form=form,
        counts=_counts(**default_counts),
        **flags,
    )


def button(name: str, **kw: Any) -> ElementDescriptor:
    return element("button", role="button", name=name, text=name, **kw)


def link(name: str, **kw: Any) -> ElementDescriptor:
    return element("a", role="link", name=name, text=name, **kw)


def textbox(label: str, *, input_type: str = "text", **kw: Any) -> ElementDescriptor:
    return element("input", role="textbox", name=label, label=label, input_type=input_type, **kw)


LOGIN_FORM = FormInfo(action=f"{BASE}/login", method="post", has_password=True, text_fields=1)
OTP_FORM = FormInfo(action=f"{BASE}/codigo", method="post", has_one_time_code=True)


def password(label: str = "Senha", **kw: Any) -> ElementDescriptor:
    kw.setdefault("form", LOGIN_FORM)
    return element(
        "input",
        role="textbox",
        name=label,
        label=label,
        input_type="password",
        is_password=True,
        sensitive=True,
        **kw,
    )


def otp(label: str = "Código de verificação", **kw: Any) -> ElementDescriptor:
    kw.setdefault("form", OTP_FORM)
    return element(
        "input",
        role="textbox",
        name=label,
        label=label,
        input_type="text",
        is_one_time_code=True,
        sensitive=True,
        **kw,
    )


def select_box(label: str, **kw: Any) -> ElementDescriptor:
    return element("select", role="combobox", name=label, label=label, **kw)


def checkbox(label: str, **kw: Any) -> ElementDescriptor:
    return element("input", role="checkbox", name=label, label=label, input_type="checkbox", **kw)


def file_input(label: str = "Arquivo", **kw: Any) -> ElementDescriptor:
    return element("input", role="button", name=label, label=label, input_type="file", **kw)


class RecordingBuilder:
    def __init__(self, base: str = BASE) -> None:
        self.base = base
        self.events: list[RecordingEvent] = []
        self.frame_url = base
        #: Tempo a mais entre eventos (``wait``), além dos 50 ms de cada um.
        self.waited_ms = 0.0

    def _add[E: BaseModel](self, cls: type[E], **data: Any) -> E:
        data.setdefault("page_id", 0)
        t_ms = len(self.events) * 50.0 + self.waited_ms
        event = cls.model_validate({"index": len(self.events), "t_ms": t_ms, **data})
        self.events.append(cast(RecordingEvent, event))
        return event

    def wait(self, ms: float) -> None:
        """A pessoa demora ``ms`` milissegundos antes do próximo evento."""
        self.waited_ms += ms

    def url(self, path: str) -> str:
        return path if path.startswith("http") else f"{self.base}{path}"

    def navigate(
        self,
        path: str,
        *,
        cause: NavigationCause = NavigationCause.TYPED,
        trigger: RecordingEvent | None = None,
        main_frame: bool = True,
        same_document: bool = False,
        page_id: int = 0,
    ) -> NavigationEvent:
        url = self.url(path)
        if main_frame:
            self.frame_url = url
        return self._add(
            NavigationEvent,
            url=url,
            main_frame=main_frame,
            cause=cause,
            trigger_index=trigger.index if trigger is not None else None,
            same_document=same_document,
            page_id=page_id,
        )

    def click(self, el: ElementDescriptor, *, detail: int = 1) -> ClickEvent:
        return self._add(ClickEvent, element=el, detail=detail, frame_url=self.frame_url)

    def click_to(self, el: ElementDescriptor, path: str, *, method: str = "GET") -> ClickEvent:
        """Clique que navega (com a requisição do documento)."""
        click = self.click(el)
        self.request(method, path)
        self.navigate(path, cause=NavigationCause.CLICK, trigger=click)
        return click

    def fill(
        self,
        el: ElementDescriptor,
        value: str | None,
        *,
        typed: str | None = None,
        script_modified: bool = False,
        editing_since: RecordingEvent | None = None,
    ) -> InputEvent:
        """Valor final do campo; ``editing_since``: a edição começou logo antes desse evento."""
        extra: dict[str, Any] = {}
        if editing_since is not None:
            extra["editing_since_ms"] = max(0.0, editing_since.t_ms - 1)
        return self._add(
            InputEvent,
            element=el,
            value=None if el.sensitive else value,
            typed=None if el.sensitive else (typed if typed is not None else value),
            script_modified=script_modified,
            frame_url=self.frame_url,
            **extra,
        )

    def select(self, el: ElementDescriptor, label: str) -> SelectEvent:
        return self._add(
            SelectEvent, element=el, label=label, value=label, frame_url=self.frame_url
        )

    def check(self, el: ElementDescriptor, checked: bool = True) -> CheckEvent:
        return self._add(CheckEvent, element=el, checked=checked, frame_url=self.frame_url)

    def file(self, el: ElementDescriptor, *names: str) -> FileEvent:
        return self._add(FileEvent, element=el, file_names=list(names), frame_url=self.frame_url)

    def key(self, key: str, el: ElementDescriptor | None = None) -> KeyEvent:
        return self._add(KeyEvent, key=key, element=el, frame_url=self.frame_url)

    def request(
        self, method: str, path: str, resource_type: str = "document", *, main_frame: bool = True
    ) -> RequestEvent:
        return self._add(
            RequestEvent,
            method=method,
            url=self.url(path),
            resource_type=resource_type,
            main_frame=main_frame,
            is_navigation=resource_type == "document",
            has_referer=True,
        )

    def download(self, filename: str, trigger: RecordingEvent | None) -> DownloadEvent:
        return self._add(
            DownloadEvent,
            url=self.url(f"/arquivos/{filename}"),
            suggested_filename=filename,
            trigger_index=trigger.index if trigger is not None else None,
        )

    def page_text(
        self,
        *headings: str,
        navigation: NavigationEvent | None = None,
        after: RecordingEvent | None = None,
        text: str = "",
    ) -> PageTextEvent:
        if navigation is None:
            navigation = next(
                e for e in reversed(self.events) if isinstance(e, NavigationEvent) and e.main_frame
            )
        return self._add(
            PageTextEvent,
            url=navigation.url,
            headings=[Heading(level=1, text=h) for h in headings],
            text=text or "\n".join(headings),
            navigation_index=navigation.index,
            after_index=after.index if after is not None else navigation.index,
        )

    def build(self, **extra: Any) -> Recording:
        return Recording(started_at=datetime.now(UTC), events=list(self.events), **extra)
