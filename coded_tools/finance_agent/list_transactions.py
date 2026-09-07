"""CodedTool for reading raw ledger entries back out over a date range."""

import sqlite3
from typing import Any

from coded_tools.finance_agent.finance_store import FinanceError
from coded_tools.finance_agent.finance_store import FinanceTool
from coded_tools.finance_agent.finance_store import month_bounds
from coded_tools.finance_agent.finance_store import normalise_category
from coded_tools.finance_agent.finance_store import parse_day
from coded_tools.finance_agent.finance_store import parse_month
from coded_tools.finance_agent.finance_store import to_major

KINDS = ("expenses", "income", "contributions", "all")
DEFAULT_LIMIT = 50
MAX_LIMIT = 200


class ListTransactions(FinanceTool):
    """
    Returns the individual entries behind a total.

    Aggregates answer "how much"; this answers "on what". Note that recurring entries
    are stored once and projected forward, so this tool reports the underlying rows
    within the window and marks the recurring ones - it does not fabricate a synthetic
    row per month. Use compute_balance for month totals that include projections.
    """

    def run(self, connection: sqlite3.Connection, args: dict[str, Any], user_id: str) -> Any:
        kind = self._kind(args.get("kind"))
        start, end = self._window(args)
        limit = self._limit(args.get("limit"))
        category = normalise_category(args["category"]) if args.get("category") else None

        result: dict[str, Any] = {"from": start, "to": end, "kind": kind}

        if kind in ("expenses", "all"):
            result["expenses"] = self._expenses(connection, user_id, start, end, category, limit)
            result["expenses_total"] = round(sum(item["amount"] for item in result["expenses"]), 2)
        if kind in ("income", "all"):
            result["income"] = self._income(connection, user_id, start, end, limit)
            result["income_total"] = round(sum(item["amount"] for item in result["income"]), 2)
        if kind in ("contributions", "all"):
            result["goal_contributions"] = self._contributions(connection, user_id, start, end, limit)
            result["goal_contributions_total"] = round(
                sum(item["amount"] for item in result["goal_contributions"]), 2
            )

        return result

    @staticmethod
    def _kind(raw: Any) -> str:
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return "expenses"
        value = str(raw).strip().lower()
        aliases = {"expense": "expenses", "spend": "expenses", "spends": "expenses", "both": "all"}
        value = aliases.get(value, value)
        if value not in KINDS:
            raise FinanceError(f"'kind' must be one of {', '.join(KINDS)}.")
        return value

    @staticmethod
    def _window(args: dict[str, Any]) -> tuple[str, str]:
        """Resolve either a 'month' shorthand or an explicit from/to pair."""
        if args.get("from_date") or args.get("to_date"):
            start = parse_day(args.get("from_date"), "from_date")
            end = parse_day(args.get("to_date"), "to_date")
            if start > end:
                raise FinanceError(f"'from_date' ({start}) is after 'to_date' ({end}).")
            return start, end
        return month_bounds(parse_month(args.get("month")))

    @staticmethod
    def _limit(raw: Any) -> int:
        try:
            return max(1, min(MAX_LIMIT, int(raw)))
        except (TypeError, ValueError):
            return DEFAULT_LIMIT

    @staticmethod
    def _expenses(
        connection: sqlite3.Connection,
        user_id: str,
        start: str,
        end: str,
        category: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT * FROM expenses
             WHERE user_id = ?
               AND (
                     (recurring = 0 AND spent_on BETWEEN ? AND ?)
                  OR (recurring = 1 AND spent_on <= ?)
               )
        """
        parameters: list[Any] = [user_id, start, end, end]
        if category:
            query += " AND category = ?"
            parameters.append(category)
        query += " ORDER BY spent_on DESC, id DESC LIMIT ?"
        parameters.append(limit)

        return [
            {
                "id": int(row["id"]),
                "category": row["category"],
                "amount": to_major(row["amount_minor"]),
                "spent_on": row["spent_on"],
                "description": row["description"],
                "recurring": bool(row["recurring"]),
            }
            for row in connection.execute(query, parameters).fetchall()
        ]

    @staticmethod
    def _income(
        connection: sqlite3.Connection, user_id: str, start: str, end: str, limit: int
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT * FROM income
             WHERE user_id = ?
               AND active = 1
               AND (
                     (frequency = 'one_time' AND starts_on BETWEEN ? AND ?)
                  OR (frequency = 'monthly'  AND starts_on <= ?)
               )
             ORDER BY starts_on DESC, id DESC
             LIMIT ?
            """,
            (user_id, start, end, end, limit),
        ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "source": row["source"],
                "amount": to_major(row["amount_minor"]),
                "frequency": row["frequency"],
                "effective_from": row["starts_on"],
                "notes": row["notes"],
            }
            for row in rows
        ]

    @staticmethod
    def _contributions(
        connection: sqlite3.Connection, user_id: str, start: str, end: str, limit: int
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT c.amount_minor, c.contributed_on, c.note, g.name AS goal_name
              FROM goal_contributions c
              JOIN goals g ON g.id = c.goal_id
             WHERE g.user_id = ?
               AND c.contributed_on BETWEEN ? AND ?
             ORDER BY c.contributed_on DESC, c.id DESC
             LIMIT ?
            """,
            (user_id, start, end, limit),
        ).fetchall()
        return [
            {
                "goal": row["goal_name"],
                "amount": to_major(row["amount_minor"]),
                "on": row["contributed_on"],
                "note": row["note"],
            }
            for row in rows
        ]
