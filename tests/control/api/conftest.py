"""Fixtures da camada HTTP (REST, MCP e autenticação)."""

import asyncio
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.control.api.support import World, build_world, make_app


@pytest.fixture
def world() -> World:
    return asyncio.run(build_world())


@pytest.fixture
async def async_world() -> World:
    return await build_world()


@pytest.fixture
def app(world: World) -> FastAPI:
    return make_app(world)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
