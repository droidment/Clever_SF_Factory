import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment

from factory.api import create_app
from factory.config import Settings
from factory.contracts import WorkInput


@pytest.fixture
def config(tmp_path):
    external = os.environ.get("FACTORY_TEST_DATABASE_URL")
    if not external:
        yield Settings(database_url=f"sqlite:///{tmp_path / 'test.db'}", demo_delay=0, _env_file=None)
        return
    url = make_url(external)
    if url.get_backend_name() != "postgresql" or url.database != "factory_test":
        raise ValueError("Container tests require a dedicated PostgreSQL database named factory_test")
    schema = "factory_test_" + uuid.uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    isolated = url.update_query_dict({"options": f"-csearch_path={schema}"})
    try:
        yield Settings(
            database_url=isolated.render_as_string(hide_password=False), demo_delay=0, _env_file=None
        )
    finally:
        # Only the schema created by this fixture in the dedicated test database is removed.
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
async def temporal_environment():
    address = os.environ.get("FACTORY_TEST_TEMPORAL_ADDRESS")
    if address:
        environment = WorkflowEnvironment.from_client(await Client.connect(address))
    else:
        environment = await WorkflowEnvironment.start_local()
    async with environment:
        yield environment


@pytest.fixture
def app(config):
    return create_app(config, dispatch=False)


@pytest.fixture
def client(app):
    with TestClient(app) as client:
        yield client


@pytest.fixture
def work():
    return WorkInput(
        title="Fix duplicate order callbacks",
        description="Fix the order callback bug and add regression coverage to prevent duplicate orders.",
        requestor="Release Engineering",
        repository="platform/order-service",
        acceptance_criteria=["Duplicate callbacks create one order", "Regression tests pass"],
    )
