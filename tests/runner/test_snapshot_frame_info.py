"""O caminho dos iframes, depois de mascarado, fica dentro do contrato do retrato."""

from uuid import uuid4

import pytest
from playwright.async_api import Browser

from rapa.core.contracts import RunState
from rapa.core.snapshot import REDACTED
from rapa.runner.result import RunResult
from rapa.runner.snapshot import capture

pytestmark = pytest.mark.browser


async def test_masked_iframe_attributes_stay_within_the_contract(browser: Browser) -> None:
    page = await browser.new_page()
    try:
        long = "ano 2024 " * 40  # cortado em 300 caracteres; cada 2024 vira REMOVIDO
        await page.set_content(
            "<button>Outro</button>"
            f"<iframe id='{long}' name='{long}' title='{long}' "
            "srcdoc='<button>Confirmar</button>'></iframe>"
        )
        await page.wait_for_function("document.querySelector('iframe').contentDocument.body")
        snapshot = await capture(
            page,
            step_id="confirmar",
            step_kind="click",
            error_code="elemento_nao_encontrado",
            mask_values=["2024"],
        )
    finally:
        await page.close()
    assert snapshot is not None and len(snapshot.frames) == 2
    info = snapshot.frames[1].path[0]
    for value in (info.id, info.name, info.title):
        assert value is not None and REDACTED in value and "2024" not in value
        assert len(value) <= 300
    # O resultado com o retrato passa na conferência do lançador (o RESULT.json lido de novo).
    result = RunResult(run_id=uuid4(), attempt=1, state=RunState.FAILED, failure_snapshot=snapshot)
    again = RunResult.model_validate_json(result.model_dump_json(indent=2))
    assert again.failure_snapshot is not None
