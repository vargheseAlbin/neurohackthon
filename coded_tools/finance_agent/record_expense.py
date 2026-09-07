"""CodedTool for logging spends, one at a time or several from a single sentence."""

import sqlite3
from datetime import datetime
from typing import Any

from coded_tools.finance_agent.finance_store import FinanceError
from coded_tools.finance_agent.finance_store import FinanceTool
from coded_tools.finance_agent.finance_store import normalise_category
from coded_tools.finance_agent.finance_store import parse_day
from coded_tools.finance_agent.finance_store import to_major
from coded_tools.finance_agent.finance_store import to_minor

MAX_ITEMS_PER_CALL = 25


class RecordExpense(FinanceTool):
    """
    Appends spends to the ledger.

    Accepts either a single expense or an "items" array, because users describe several
    spends in one breath ("500 on groceries and 1200 on fuel yesterday") and one call
    beats three round trips through the agent chain.

    A recurring expense (rent, EMI, subscriptions) is recorded once and counted in every
    month from its start date onward, mirroring how recurring income works.
    """

    def run(self, connection: sqlite3.Connection, args: dict[str, Any], user_id: str) -> Any:
        if args.get("id") is not None and args.get("delete"):
            return self._delete(connection, args, user_id)

        items = self._collect(args)
        recorded: list[dict[str, Any]] = []
        now = datetime.now().isoformat(timespec="seconds")

        for item in items:
            category = normalise_category(item.get("category"))
            amount_minor = to_minor(item.get("amount"))
            spent_on = parse_day(item.get("spent_on"), "spent_on")
            recurring = bool(item.get("recurring"))
            description = str(item["description"]).strip() if item.get("description") else None

            cursor = connection.execute(
                """
                INSERT INTO expenses (user_id, category, amount_minor, spent_on, description, recurring, created_at)
                     VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, category, amount_minor, spent_on, description, int(recurring), now),
            )
            recorded.append(
                {
                    "id": int(cursor.lastrowid),
                    "category": category,
                    "amount": to_major(amount_minor),
                    "spent_on": spent_on,
                    "recurring": recurring,
                    "description": description,
                }
            )

        return {
            "recorded": "expenses",
            "count": len(recorded),
            "total_amount": round(sum(item["amount"] for item in recorded), 2),
            "expenses": recorded,
        }

    @staticmethod
    def _collect(args: dict[str, Any]) -> list[dict[str, Any]]:
        """Normalise the single-expense and batch call shapes into one list."""
        raw_items = args.get("items")
        if raw_items is None:
            return [args]

        if not isinstance(raw_items, list):
            raise FinanceError("'items' must be a list of expenses.")
        items = [item for item in raw_items if isinstance(item, dict)]
        if not items:
            raise FinanceError("'items' was empty. Pass at least one expense with a category and amount.")
        if len(items) > MAX_ITEMS_PER_CALL:
            raise FinanceError(f"Too many expenses in one call ({len(items)}). Send at most {MAX_ITEMS_PER_CALL}.")
        return items

    @staticmethod
    def _delete(connection: sqlite3.Connection, args: dict[str, Any], user_id: str) -> dict[str, Any]:
        """Remove a mistakenly logged expense by the id that was handed back on insert."""
        try:
            expense_id = int(args["id"])
        except (TypeError, ValueError) as error:
            raise FinanceError("'id' must be the numeric id of the expense to delete.") from error

        row = connection.execute(
            "SELECT * FROM expenses WHERE user_id = ? AND id = ?", (user_id, expense_id)
        ).fetchone()
        if row is None:
            raise FinanceError(f"No expense with id {expense_id}.")

        connection.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
        return {
            "deleted": "expense",
            "id": expense_id,
            "category": row["category"],
            "amount": to_major(row["amount_minor"]),
            "spent_on": row["spent_on"],
        }
