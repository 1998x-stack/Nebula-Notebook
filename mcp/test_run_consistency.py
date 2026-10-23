"""Unit tests for notebook outputs under concurrent agent edits (no live server)."""
import unittest
from unittest.mock import AsyncMock

from nebula_client import NebulaClient, Conflict, NebulaError


def notebook(source="x = 1", version=1):
    return {
        "id": "notebook-1", "version": version, "title": "Test",
        "document": {"cells": [{
            "id": "cell-1", "cell_type": "code", "source": source,
            "outputs": [], "execution_count": None,
        }]},
    }


def response():
    return {
        "status": "ok", "timed_out": False, "execution_count": 1,
        "outputs": [{"output_type": "stream", "name": "stdout", "text": "1\n"}],
    }


def client_fixture():
    client = NebulaClient.__new__(NebulaClient)
    client.execute = AsyncMock(return_value=response())
    client.save_notebook = AsyncMock(return_value={"version": 3})
    return client


class RunConsistencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_cell_does_not_attach_output_to_new_source(self):
        client = client_fixture()
        client.get_notebook = AsyncMock(side_effect=[notebook(), notebook("x = 2")])
        with self.assertRaisesRegex(Conflict, "source changed"):
            await client.run_cell("notebook-1")
        client.save_notebook.assert_not_awaited()

    async def test_run_window_requires_progress(self):
        client = client_fixture()
        client.get_notebook = AsyncMock()
        with self.assertRaisesRegex(NebulaError, "positive integer"):
            await client.run_cells("notebook-1", count=0)
        client.get_notebook.assert_not_awaited()

    async def test_run_window_preserves_outputs_across_409(self):
        client = client_fixture()
        client.get_notebook = AsyncMock(side_effect=[notebook(), notebook(), notebook(version=2)])
        client.save_notebook = AsyncMock(side_effect=[Conflict("409 conflict"), {"version": 3}])
        result = await client.run_cells("notebook-1", count=1)
        self.assertEqual(result["saved_version"], 3)
        self.assertEqual(client.save_notebook.await_count, 2)
        for call in client.save_notebook.await_args_list:
            self.assertEqual(call.args[3]["cells"][0]["outputs"][0]["text"], "1\n")

    async def test_shared_cell_update_requires_matching_version(self):
        client = client_fixture()
        # The mutator callback sees a later version, like a competing Agent save.
        async def mutate(notebook_id, apply):
            current = notebook(source="from_other_agent = True", version=3)
            apply(current["document"], current)
            return None, 4
        client.mutate = mutate
        with self.assertRaisesRegex(Conflict, "Notebook changed"):
            await client.update_cell("notebook-1", cell_id="cell-1",
                                     source="my_new_value = 1", expected_version=2)

    async def test_old_cell_update_remains_supported_without_expected_version(self):
        client = client_fixture()
        async def mutate(notebook_id, apply):
            current = notebook(version=3)
            apply(current["document"], current)
            self.assertEqual(current["document"]["cells"][0]["source"], "accepted = 1")
            return None, 4
        client.mutate = mutate
        result = await client.update_cell("notebook-1", cell_id="cell-1",
                                          source="accepted = 1")
        self.assertEqual(result["version"], 4)

    async def test_run_all_rejects_modified_cell(self):
        client = client_fixture()
        client.get_notebook = AsyncMock(side_effect=[notebook(), notebook("x = 2")])
        with self.assertRaisesRegex(Conflict, "source changed"):
            await client.run_all("notebook-1")
        client.save_notebook.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
