"""Runs table access."""

from __future__ import annotations

from typing import Any

from eap_core.db import Database, Tx, dumps, loads, now_iso

JSON_COLS = ("input", "output", "error", "steps", "sources", "needs_input", "state", "flags", "feedback")


class RunStore:
    def __init__(self, db: Database):
        self.db = db

    async def create(self, row: dict[str, Any]) -> None:
        ts = now_iso()
        row = {"created_at": ts, "updated_at": ts, **row}
        for k in JSON_COLS:
            if k in row and row[k] is not None:
                row[k] = dumps(row[k])
        cols = list(row)

        def run(tx: Tx) -> None:
            tx.execute(f"INSERT INTO runs ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                       [row[c] for c in cols])

        await self.db.call(run)

    async def update(self, run_id: str, **fields: Any) -> None:
        fields["updated_at"] = now_iso()
        for k in JSON_COLS:
            if k in fields and fields[k] is not None:
                fields[k] = dumps(fields[k])
        sets = ", ".join(f"{k} = ?" for k in fields)

        def run(tx: Tx) -> None:
            tx.execute(f"UPDATE runs SET {sets} WHERE id = ?", [*fields.values(), run_id])

        await self.db.call(run)

    async def get(self, run_id: str) -> dict[str, Any] | None:
        row = await self.db.call(lambda tx: tx.fetchone("SELECT * FROM runs WHERE id = ?", (run_id,)))
        return self._decode(row)

    async def by_idempotency_key(self, key: str) -> dict[str, Any] | None:
        row = await self.db.call(lambda tx: tx.fetchone("SELECT * FROM runs WHERE idempotency_key = ?", (key,)))
        return self._decode(row)

    async def feedback(self, run_id: str, rating: str, comment: str, by: str) -> None:
        fb = {"rating": rating, "comment": comment[:2000], "by": by, "at": now_iso()}

        def run(tx: Tx) -> None:
            tx.execute("UPDATE runs SET feedback = ?, updated_at = ? WHERE id = ?", (dumps(fb), now_iso(), run_id))
            if rating == "down":  # a thumbs-down needs a look, unless already handled
                tx.execute("UPDATE runs SET review_status = 'open' WHERE id = ? AND review_status IS NULL", (run_id,))

        await self.db.call(run)

    async def review(self, run_id: str, *, status: str | None, assignee: str | None, note: str | None,
                     by: str) -> None:
        fields: dict[str, Any] = {"reviewed_by": by, "reviewed_at": now_iso()}
        if status is not None:
            fields["review_status"] = status
        if assignee is not None:
            fields["assignee"] = assignee or None
        if note is not None:
            fields["review_note"] = note[:4000]
        await self.update(run_id, **fields)

    async def search(self, *, agents: list[str] | None = None, bus: list[str] | None = None,
                     status: str | None = None, flag: str | None = None, review: str | None = None,
                     feedback: str | None = None, q: str | None = None, since: str | None = None,
                     include_tests: bool = False, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        where, args = ["1 = 1"], []
        if agents:
            where.append(f"agent IN ({', '.join('?' for _ in agents)})"); args += agents
        if bus is not None:
            if not bus:
                return []
            where.append(f"bu IN ({', '.join('?' for _ in bus)})"); args += bus
        if status:
            where.append("status = ?"); args.append(status)
        if flag:
            where.append("flags LIKE ?"); args.append(f'%"{flag}%')
        if review == "any":
            where.append("review_status IS NOT NULL")
        elif review:
            where.append("review_status = ?"); args.append(review)
        if feedback:
            where.append("feedback LIKE ?"); args.append(f'%"rating": "{feedback}"%')
        if q:
            where.append("(input LIKE ? OR output LIKE ? OR id = ? OR trace_id = ?)"); args += [f"%{q}%", f"%{q}%", q, q]
        if since:
            where.append("created_at >= ?"); args.append(since)
        if not include_tests:
            where.append("mode <> 'execute'")
        sql = (f"SELECT * FROM runs WHERE {' AND '.join(where)} ORDER BY created_at DESC "
               f"LIMIT {max(1, min(int(limit), 500))} OFFSET {max(0, int(offset))}")
        rows = await self.db.call(lambda tx: tx.fetchall(sql, args))
        return [self._decode(r) for r in rows]  # type: ignore[misc]

    @staticmethod
    def _decode(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        for k in JSON_COLS:
            row[k] = loads(row.get(k))
        return row

    async def record_response(self, resp: Any, *, agent: str, alias: str | None, mode: str, input: Any,
                              principal: str, on_behalf_of: str, bu: str | None, depth: int,
                              state: Any = None, idempotency_key: str | None = None, exists: bool = False) -> None:
        fields = {
            "status": resp.status,
            "output": resp.output,
            "error": resp.error.model_dump() if resp.error else None,
            "cost_usd": resp.cost.usd,
            "input_tokens": resp.cost.input_tokens,
            "output_tokens": resp.cost.output_tokens,
            "latency_ms": resp.latency_ms,
            "steps": [s.model_dump() for s in resp.trace.steps],
            "sources": [s.model_dump() for s in resp.sources],
            "needs_input": resp.needs_input.model_dump() if resp.needs_input else None,
            "state": state,
            "version": resp.trace.version,
            "content_hash": resp.trace.content_hash,
            "context_id": getattr(resp, "context", None),
            "flags": list(getattr(resp, "flags", []) or []),
        }
        if "escalated" in fields["flags"]:
            fields["review_status"] = "open"  # lands in the Monitor inbox
        if exists:
            await self.update(resp.run_id, **fields)
        else:
            await self.create({
                "id": resp.run_id, "agent": agent, "alias": alias, "mode": mode, "input": input,
                "trace_id": resp.trace.trace_id, "parent_run_id": resp.trace.parent_run_id,
                "principal": principal, "on_behalf_of": on_behalf_of, "bu": bu, "depth": depth,
                "idempotency_key": idempotency_key, **fields,
            })
