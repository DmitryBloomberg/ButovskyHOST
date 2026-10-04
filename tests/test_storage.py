import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from storage import JsonStorage


class JsonStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.admin_id = 9001
        self.store = JsonStorage(Path(self.temp_dir.name) / "data", frozenset({self.admin_id}))

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_user_is_created_and_admin_status_is_bootstrapped(self):
        user = await self.store.get_or_create_user(
            SimpleNamespace(id=123, first_name="Имя", last_name="Тест", username="test_user")
        )
        admin = await self.store.get_or_create_user(
            SimpleNamespace(id=self.admin_id, first_name="Админ", last_name="", username=None)
        )

        self.assertEqual(user["status"], "U")
        self.assertEqual(user["balance"], 0)
        self.assertTrue((Path(self.temp_dir.name) / "data/users/123.json").exists())
        self.assertEqual(admin["status"], "A")
        self.assertEqual(admin["username"], "")

    async def test_user_can_be_blocked_and_unblocked(self):
        await self.store.get_or_create_user(
            SimpleNamespace(id=123, first_name="Имя", last_name="", username=None)
        )
        await self.store.get_or_create_user(
            SimpleNamespace(id=self.admin_id, first_name="Админ", last_name="", username=None)
        )

        blocked = await self.store.set_user_status(123, "B")
        unblocked = await self.store.set_user_status(123, "U")
        protected_admin = await self.store.set_user_status(self.admin_id, "B")

        self.assertEqual(blocked["status"], "B")
        self.assertEqual(unblocked["status"], "U")
        self.assertEqual(protected_admin["status"], "A")

    async def test_tariff_visibility_and_order_status_transition(self):
        tariff = {"id": "trial", "name": "Тест", "active": True}
        await self.store.add_tariff(tariff)
        self.assertEqual(len(await self.store.list_tariffs(active_only=True)), 1)

        hidden = await self.store.toggle_tariff("trial")
        self.assertFalse(hidden["active"])
        self.assertEqual(await self.store.list_tariffs(active_only=True), [])

        order = {"id": "ABCD1234", "user_id": 123, "status": "requested", "created_at": "2026-01-01"}
        await self.store.create_order(order)
        updated = await self.store.update_order(
            "ABCD1234", allowed_statuses={"requested"}, changes={"status": "in_work"}
        )
        duplicate_transition = await self.store.update_order(
            "ABCD1234", allowed_statuses={"requested"}, changes={"status": "cancelled"}
        )

        self.assertEqual(updated["status"], "in_work")
        self.assertIsNone(duplicate_transition)
        self.assertEqual((await self.store.latest_order_for_user(123, {"in_work"}))["id"], "ABCD1234")


if __name__ == "__main__":
    unittest.main()