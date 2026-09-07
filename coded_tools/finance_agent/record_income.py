"""CodedTool for adding, updating and stopping the user's income streams."""

import sqlite3
from datetime import datetime
from typing import Any

from coded_tools.finance_agent.finance_store import FREQUENCIES
from coded_tools.finance_agent.finance_store import FinanceError
from coded_tools.finance_agent.finance_store import FinanceTool
from coded_tools.finance_agent.finance_store import parse_choice
from coded_tools.finance_agent.finance_store import parse_day
from coded_tools.finance_agent.finance_store import to_major
from coded_tools.finance_agent.finance_store import to_minor

ACTIONS = ("add", "update", "stop", "list")


class RecordIncome(FinanceTool):
    """
    Maintains the income side of the ledger: salary, freelance work, rent, dividends.

    A 'monthly' stream is recorded once and then counted in every later month, so the
    user states their salary a single time rather than re-entering it every payday.
    """

    def run(self, connection: sqlite3.Connection, args: dict[str, Any], user_id: str) -> Any:
        action = parse_choice(args.get("action"), ACTIONS, "action", "add")

        if action == "list":
            return self._list(connection, user_id)
        if action == "add":
            return self._add(connection, args, user_id)
        if action == "update":
            return self._update(connection, args, user_id)
        return self._stop(connection, args, user_id)

    def _add(self, connection: sqlite3.Connection, args: dict[str, Any], user_id: str) -> dict[str, Any]:
        source = str(args.get("source") or "").strip()
        if not source:
            raise FinanceError("'source' is required, e.g. salary, freelance, rental income.")

        amount_minor = to_minor(args.get("amount"))
        frequency = parse_choice(args.get("frequency"), FREQUENCIES, "frequency", "monthly")
        starts_on = parse_day(args.get("received_on"), "received_on")

        existing = self._find(connection, user_id, source)
        if existing is not None and existing["frequency"] == "monthly" and frequency == "monthly":
            # Re-stating a recurring stream is a raise, not a second salary. Adding a
            # duplicate row here would silently double the user's reported income.
            return self._apply_update(
                connection,
                existing,
                amount_minor=amount_minor,
                starts_on=starts_on,
                notes=args.get("notes"),
                note="An existing monthly stream with this name was updated rather than duplicated.",
            )

        cursor = connection.execute(
            """
            INSERT INTO income (user_id, source, amount_minor, frequency, starts_on, active, notes, created_at)
                 VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                user_id,
                source,
                amount_minor,
                frequency,
                starts_on,
                (str(args["notes"]).strip() if args.get("notes") else None),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        return {
            "recorded": "income",
            "id": int(cursor.lastrowid),
            "source": source,
            "amount": to_major(amount_minor),
            "frequency": frequency,
            "effective_from": starts_on,
        }

    def _update(self, connection: sqlite3.Connection, args: dict[str, Any], user_id: str) -> dict[str, Any]:
        row = self._require(connection, user_id, args.get("source"))
        if args.get("amount") is None and not args.get("notes") and not args.get("received_on"):
            raise FinanceError("Nothing to update. Pass a new 'amount', 'received_on' or 'notes'.")
        return self._apply_update(
            connection,
            row,
            amount_minor=to_minor(args["amount"]) if args.get("amount") is not None else None,
            starts_on=parse_day(args["received_on"], "received_on") if args.get("received_on") else None,
            notes=args.get("notes"),
        )

    def _apply_update(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        amount_minor: int | None = None,
        starts_on: str | None = None,
        notes: Any = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Write the non-None fields onto an existing income row and report the delta."""
        previous_minor = int(row["amount_minor"])
        new_minor = previous_minor if amount_minor is None else amount_minor

        connection.execute(
            """
            UPDATE income
               SET amount_minor = ?,
                   starts_on    = COALESCE(?, starts_on),
                   notes        = COALESCE(?, notes),
                   active       = 1
             WHERE id = ?
            """,
            (new_minor, starts_on, (str(notes).strip() if notes else None), int(row["id"])),
        )

        result: dict[str, Any] = {
            "updated": "income",
            "id": int(row["id"]),
            "source": row["source"],
            "previous_amount": to_major(previous_minor),
            "amount": to_major(new_minor),
            "change": to_major(new_minor - previous_minor),
            "frequency": row["frequency"],
        }
        if starts_on:
            result["effective_from"] = starts_on
        if note:
            result["note"] = note
        return result

    def _stop(self, connection: sqlite3.Connection, args: dict[str, Any], user_id: str) -> dict[str, Any]:
        row = self._require(connection, user_id, args.get("source"))
        connection.execute("UPDATE income SET active = 0 WHERE id = ?", (int(row["id"]),))
        return {
            "stopped": "income",
            "id": int(row["id"]),
            "source": row["source"],
            "note": "This stream no longer counts toward any month's income.",
        }

    @staticmethod
    def _list(connection: sqlite3.Connection, user_id: str) -> dict[str, Any]:
        rows = connection.execute(
            "SELECT * FROM income WHERE user_id = ? ORDER BY active DESC, amount_minor DESC",
            (user_id,),
        ).fetchall()
        monthly_total = sum(int(r["amount_minor"]) for r in rows if r["active"] and r["frequency"] == "monthly")
        return {
            "income_streams": [
                {
                    "id": int(r["id"]),
                    "source": r["source"],
                    "amount": to_major(r["amount_minor"]),
                    "frequency": r["frequency"],
                    "effective_from": r["starts_on"],
                    "active": bool(r["active"]),
                    "notes": r["notes"],
                }
                for r in rows
            ],
            "recurring_monthly_total": to_major(monthly_total),
        }

    @staticmethod
    def _find(connection: sqlite3.Connection, user_id: str, source: str) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM income WHERE user_id = ? AND lower(trim(source)) = ? ORDER BY active DESC, id DESC",
            (user_id, source.strip().lower()),
        ).fetchone()

    def _require(self, connection: sqlite3.Connection, user_id: str, source: Any) -> sqlite3.Row:
        text = str(source or "").strip()
        if not text:
            raise FinanceError("'source' is required to identify which income stream to change.")
        row = self._find(connection, user_id, text)
        if row is not None:
            return row
        existing = connection.execute(
            "SELECT source FROM income WHERE user_id = ? ORDER BY source", (user_id,)
        ).fetchall()
        if not existing:
            raise FinanceError("No income has been recorded yet.")
        raise FinanceError(
            f"No income stream called '{text}'. Known streams: {', '.join(r['source'] for r in existing)}."
        )
