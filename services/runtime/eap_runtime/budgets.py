"""Atomic spend counters in Postgres/SQLite.

reserve():  conditional UPDATE ... WHERE spent + reserved + amount <= limit  (atomic)
settle():   move a reservation to actual spend (actual may differ from the estimate)

Because the check and the increment are one statement, concurrent runtime instances can
never collectively overshoot a limit by more than the difference between estimates and
actuals of in-flight calls.
"""

from __future__ import annotations

from datetime import datetime, timezone

from eap_core.db import Database, Tx, now_iso


class BudgetExhausted(Exception):
    pass


def bu_day_key(bu: str | None) -> str:
    return f"bu:{bu or 'unknown'}:{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"


class Budgets:
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _ensure(tx: Tx, key: str, limit: float | None) -> None:
        tx.execute(
            "INSERT INTO budgets (key, limit_usd, spent_usd, reserved_usd, updated_at) VALUES (?, ?, 0, 0, ?) "
            "ON CONFLICT (key) DO NOTHING",
            (key, limit, now_iso()),
        )
        # policy limit may have changed since the row was created
        tx.execute("UPDATE budgets SET limit_usd = ? WHERE key = ?", (limit, key))

    async def reserve(self, key: str, amount: float, limit: float | None) -> None:
        def run(tx: Tx) -> int:
            self._ensure(tx, key, limit)
            return tx.execute(
                "UPDATE budgets SET reserved_usd = reserved_usd + ?, updated_at = ? "
                "WHERE key = ? AND (limit_usd IS NULL OR spent_usd + reserved_usd + ? <= limit_usd)",
                (amount, now_iso(), key, amount),
            )

        if await self.db.call(run) != 1:
            raise BudgetExhausted(f"budget {key} exhausted")

    async def settle(self, key: str, reserved: float, actual: float) -> None:
        def run(tx: Tx) -> None:
            tx.execute(
                "UPDATE budgets SET reserved_usd = CASE WHEN reserved_usd - ? < 0 THEN 0 ELSE reserved_usd - ? END, "
                "spent_usd = spent_usd + ?, updated_at = ? WHERE key = ?",
                (reserved, reserved, actual, now_iso(), key),
            )

        await self.db.call(run)

    async def available(self, key: str, limit: float | None) -> float:
        """Unreserved headroom under `limit` (inf when unlimited). Advisory: reserve() is the atomic check."""
        if limit is None:
            return float("inf")
        row = await self.get(key)
        if row is None:
            return float(limit)
        return max(0.0, float(limit) - row["spent_usd"] - row["reserved_usd"])

    async def get(self, key: str) -> dict | None:
        return await self.db.call(lambda tx: tx.fetchone("SELECT * FROM budgets WHERE key = ?", (key,)))
