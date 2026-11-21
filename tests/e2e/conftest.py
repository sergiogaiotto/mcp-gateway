"""Fixtures de ponta a ponta: o serviço inteiro sobre PostgreSQL, Chromium e o portal."""

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

from rapa.control.db import Database
from rapa.fixtures.server import HOST_RESOLVER_ARG, RunningPortal
from rapa.server import ServerSettings, create_server_app
from tests.control.db.conftest import app_db, pg_database, superuser_engine  # noqa: F401
from tests.control.db.support import PgDatabase, Tenant, make_tenant, reset_queue
from tests.e2e.support import SECRETS, TOKENS


@pytest.fixture
async def platform_app(
    app_db: Database,  # noqa: F811
    pg_database: PgDatabase,  # noqa: F811
    superuser_engine: Any,  # noqa: F811
    portal_v1: RunningPortal,
    chromium_path: str,
    tmp_path: Path,
) -> AsyncIterator[tuple[FastAPI, Tenant, Tenant]]:
    await reset_queue(superuser_engine, total_slots=4)
    tenant = await make_tenant(app_db, "Ponta a ponta")
    other = await make_tenant(app_db, "Outra empresa")
    tokens = {
        TOKENS["creator"]: tenant.creator,
        TOKENS["approver"]: tenant.approver,
        TOKENS["operator"]: tenant.operator,
        TOKENS["outsider"]: tenant.outsider,
        TOKENS["other"]: other.admin,
    }
    tokens_file = tmp_path / "tokens.json"
    tokens_file.write_text(
        json.dumps(
            {t: {"sujeito": p.subject, "empresa": str(p.company_id)} for t, p in tokens.items()}
        )
    )
    secrets_file = tmp_path / "segredos.json"
    secrets_file.write_text(json.dumps({str(tenant.company_id): SECRETS}))
    settings = ServerSettings(
        database_url=pg_database.app_url,
        dispatcher_database_url=pg_database.dispatcher_url,
        ui=False,
        dev=True,
        dev_tokens_file=tokens_file,
        dev_secrets_file=secrets_file,
        rate_limit_per_s=None,
        poll_interval_s=0.2,
        work_dir=tmp_path / "trabalho",
        artifacts_dir=tmp_path / "artefatos",
        chromium=chromium_path,
        browser_args=(HOST_RESOLVER_ARG,),
        sessions_dir=tmp_path / "sessoes",
    )
    # O lifespan (gateway MCP e despachante) roda dentro do teste: o SDK do MCP
    # exige abrir e fechar o grupo de tarefas na mesma tarefa.
    yield create_server_app(settings), tenant, other
