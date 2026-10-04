from __future__ import annotations

import asyncio
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JsonStorage:
    """Single-process, atomic JSON storage for a small polling bot."""

    def __init__(self, data_dir: Path, admin_ids: frozenset[int]) -> None:
        self.data_dir = data_dir
        self.users_dir = data_dir / "users"
        self.orders_dir = data_dir / "orders"
        self.tariffs_path = data_dir / "tariffs.json"
        self.admin_ids = admin_ids
        self._lock = asyncio.Lock()
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.users_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.orders_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        for directory in (self.data_dir, self.users_dir, self.orders_dir):
            try:
                os.chmod(directory, 0o700)
            except OSError:
                pass
        if not self.tariffs_path.exists():
            self._write_json(self.tariffs_path, [])

    @staticmethod
    def _read_json(path: Path, default: Any = None) -> Any:
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)

    @staticmethod
    def _write_json(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(value, file, ensure_ascii=False, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _user_path(self, telegram_id: int) -> Path:
        return self.users_dir / f"{int(telegram_id)}.json"

    async def get_or_create_user(self, telegram_user: Any) -> dict[str, Any]:
        async with self._lock:
            path = self._user_path(telegram_user.id)
            user = self._read_json(path, None) or {
                "telegram_id": telegram_user.id,
                "first_name": telegram_user.first_name or "",
                "last_name": telegram_user.last_name or "",
                "username": telegram_user.username or "",
                "status": "U",
                "balance": 0,
                "created_at": utc_now(),
            }
            user.update(
                {
                    "telegram_id": telegram_user.id,
                    "first_name": telegram_user.first_name or "",
                    "last_name": telegram_user.last_name or "",
                    "username": telegram_user.username or "",
                    "updated_at": utc_now(),
                }
            )
            if telegram_user.id in self.admin_ids:
                user["status"] = "A"
            elif user.get("status") not in {"U", "B"}:
                user["status"] = "U"
            user["balance"] = user.get("balance", 0)
            self._write_json(path, user)
            return user

    async def get_user(self, telegram_id: int) -> dict[str, Any] | None:
        async with self._lock:
            return self._read_json(self._user_path(telegram_id), None)

    async def list_users(self) -> list[dict[str, Any]]:
        async with self._lock:
            users = []
            for path in self.users_dir.glob("*.json"):
                try:
                    item = self._read_json(path, None)
                    if item:
                        users.append(item)
                except (OSError, json.JSONDecodeError):
                    continue
            return sorted(users, key=lambda item: item.get("created_at", ""), reverse=True)

    async def set_user_status(self, telegram_id: int, status: str) -> dict[str, Any] | None:
        if status not in {"U", "B"}:
            raise ValueError("Only U and B may be assigned manually.")
        async with self._lock:
            if telegram_id in self.admin_ids:
                return self._read_json(self._user_path(telegram_id), None)
            path = self._user_path(telegram_id)
            user = self._read_json(path, None)
            if user is None:
                return None
            if user.get("status") == "A":
                return user
            user["status"] = status
            user["updated_at"] = utc_now()
            self._write_json(path, user)
            return user

    async def list_tariffs(self, active_only: bool = False) -> list[dict[str, Any]]:
        async with self._lock:
            tariffs = self._read_json(self.tariffs_path, [])
            if active_only:
                tariffs = [item for item in tariffs if item.get("active", True)]
            return sorted(tariffs, key=lambda item: item.get("created_at", ""))

    async def get_tariff(self, tariff_id: str) -> dict[str, Any] | None:
        async with self._lock:
            return next(
                (item for item in self._read_json(self.tariffs_path, []) if item.get("id") == tariff_id),
                None,
            )

    async def add_tariff(self, tariff: dict[str, Any]) -> None:
        async with self._lock:
            tariffs = self._read_json(self.tariffs_path, [])
            tariffs.append(tariff)
            self._write_json(self.tariffs_path, tariffs)

    async def toggle_tariff(self, tariff_id: str) -> dict[str, Any] | None:
        async with self._lock:
            tariffs = self._read_json(self.tariffs_path, [])
            for tariff in tariffs:
                if tariff.get("id") == tariff_id:
                    tariff["active"] = not tariff.get("active", True)
                    self._write_json(self.tariffs_path, tariffs)
                    return tariff
            return None

    def _order_path(self, order_id: str) -> Path:
        safe_id = "".join(char for char in order_id if char.isalnum() or char in "-_")
        if not safe_id:
            raise ValueError("Invalid order ID.")
        return self.orders_dir / f"{safe_id}.json"

    async def create_order(self, order: dict[str, Any]) -> None:
        async with self._lock:
            self._write_json(self._order_path(order["id"]), order)

    async def get_order(self, order_id: str) -> dict[str, Any] | None:
        async with self._lock:
            return self._read_json(self._order_path(order_id), None)

    async def update_order(
        self,
        order_id: str,
        *,
        allowed_statuses: set[str] | None = None,
        changes: dict[str, Any],
    ) -> dict[str, Any] | None:
        async with self._lock:
            path = self._order_path(order_id)
            order = self._read_json(path, None)
            if order is None:
                return None
            if allowed_statuses is not None and order.get("status") not in allowed_statuses:
                return None
            order.update(changes)
            order["updated_at"] = utc_now()
            self._write_json(path, order)
            return order

    async def list_orders(self) -> list[dict[str, Any]]:
        async with self._lock:
            orders = []
            for path in self.orders_dir.glob("*.json"):
                try:
                    item = self._read_json(path, None)
                    if item:
                        orders.append(item)
                except (OSError, json.JSONDecodeError):
                    continue
            return sorted(orders, key=lambda item: item.get("created_at", ""), reverse=True)

    async def latest_order_for_user(
        self, telegram_id: int, statuses: set[str]
    ) -> dict[str, Any] | None:
        orders = await self.list_orders()
        return next(
            (
                order
                for order in orders
                if order.get("user_id") == telegram_id and order.get("status") in statuses
            ),
            None,
        )