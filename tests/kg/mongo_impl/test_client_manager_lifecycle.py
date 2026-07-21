"""Tests for ClientManager connection lifecycle (no MongoDB instance required).

These tests verify that ClientManager properly closes the underlying
AsyncMongoClient when all references are released, and keeps it alive
while references remain.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

pytest.importorskip(
    "pymongo",
    reason="pymongo is required for Mongo ClientManager tests",
)

from lightrag.kg.mongo_impl import ClientManager


class TestClientManagerLifecycle:
    """Verify ClientManager connection open/close behavior."""

    def _reset_manager(self):
        """Reset ClientManager class state between tests."""
        ClientManager._instances = {}

    def teardown_method(self):
        self._reset_manager()

    @pytest.mark.asyncio
    async def test_clients_are_keyed_by_physical_uri_and_database(self):
        first_client = MagicMock()
        first_client.close = AsyncMock()
        first_db = MagicMock()
        first_client.get_database.return_value = first_db
        second_client = MagicMock()
        second_client.close = AsyncMock()
        second_db = MagicMock()
        second_client.get_database.return_value = second_db

        with patch(
            "lightrag.kg.mongo_impl.AsyncMongoClient",
            side_effect=[first_client, second_client],
        ) as constructor:
            first = await ClientManager.get_client(
                {"uri": "mongodb://mongo-a:27017", "database": "rag_a"}
            )
            first_again = await ClientManager.get_client(
                {"uri": "mongodb://mongo-a:27017", "database": "rag_a"}
            )
            second = await ClientManager.get_client(
                {"uri": "mongodb://mongo-b:27017", "database": "rag_b"}
            )

        assert first is first_db
        assert first_again is first_db
        assert second is second_db
        assert constructor.call_count == 2
        assert len(ClientManager._instances) == 2

    @pytest.mark.asyncio
    async def test_release_client_closes_connection_when_ref_count_zero(self):
        """When ref_count drops to 0, the MongoClient should be closed and cleared."""
        mock_client = AsyncMock()
        mock_db = MagicMock()

        resource = ("mongodb://one", "rag")
        ClientManager._instances = {
            resource: {"client": mock_client, "db": mock_db, "ref_count": 1}
        }

        await ClientManager.release_client(mock_db)

        mock_client.close.assert_awaited_once()
        assert ClientManager._instances == {}

    @pytest.mark.asyncio
    async def test_release_client_keeps_connection_with_multiple_refs(self):
        """When other references exist, the MongoClient must NOT be closed."""
        mock_client = AsyncMock()
        mock_db = MagicMock()

        resource = ("mongodb://one", "rag")
        ClientManager._instances = {
            resource: {"client": mock_client, "db": mock_db, "ref_count": 3}
        }

        await ClientManager.release_client(mock_db)

        mock_client.close.assert_not_awaited()
        assert ClientManager._instances[resource]["ref_count"] == 2
        assert ClientManager._instances[resource]["client"] is mock_client
        assert ClientManager._instances[resource]["db"] is mock_db

    @pytest.mark.asyncio
    async def test_release_client_noop_for_wrong_db(self):
        """Releasing a db that is not the tracked instance should do nothing."""
        mock_client = AsyncMock()
        mock_db = MagicMock()
        other_db = MagicMock()

        resource = ("mongodb://one", "rag")
        ClientManager._instances = {
            resource: {"client": mock_client, "db": mock_db, "ref_count": 1}
        }

        await ClientManager.release_client(other_db)

        mock_client.close.assert_not_awaited()
        assert ClientManager._instances[resource]["ref_count"] == 1

    @pytest.mark.asyncio
    async def test_release_client_noop_for_none(self):
        """Releasing None should be a safe no-op."""
        mock_client = AsyncMock()
        mock_db = MagicMock()

        resource = ("mongodb://one", "rag")
        ClientManager._instances = {
            resource: {"client": mock_client, "db": mock_db, "ref_count": 1}
        }

        await ClientManager.release_client(None)

        mock_client.close.assert_not_awaited()
        assert ClientManager._instances[resource]["ref_count"] == 1
