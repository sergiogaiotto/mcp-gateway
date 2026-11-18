"""Gravador da demonstração feita pela pessoa no navegador.

``Recorder`` grava um ``BrowserContext`` do Playwright e devolve uma
``Recording``: só dado, sem segredos, pronta para o compilador
(``rapa.compiler.lower``).
"""

from typing import TYPE_CHECKING

from rapa.recorder.models import (
    RECORDING_VERSION,
    REDACTED,
    USER_ACTION_TYPES,
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
    LocatorProbe,
    NavigationCause,
    NavigationEvent,
    PageTextEvent,
    Recording,
    RecordingEvent,
    RequestEvent,
    RowInfo,
    SelectEvent,
)

if TYPE_CHECKING:
    from rapa.recorder.recorder import Recorder, RecorderError

__all__ = [
    "RECORDING_VERSION",
    "REDACTED",
    "USER_ACTION_TYPES",
    "CheckEvent",
    "ClickEvent",
    "DownloadEvent",
    "ElementDescriptor",
    "FileEvent",
    "FormInfo",
    "FrameInfo",
    "Heading",
    "InputEvent",
    "KeyEvent",
    "LocatorCounts",
    "LocatorProbe",
    "NavigationCause",
    "NavigationEvent",
    "PageTextEvent",
    "Recorder",
    "RecorderError",
    "Recording",
    "RecordingEvent",
    "RequestEvent",
    "RowInfo",
    "SelectEvent",
]


def __getattr__(name: str) -> object:
    # O Recorder depende do Playwright (extra "runner"); o modelo da gravação não.
    if name in ("Recorder", "RecorderError"):
        from rapa.recorder import recorder

        return getattr(recorder, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
