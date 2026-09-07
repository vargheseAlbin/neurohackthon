"""CodedTool that turns the ledger into a month's income, outgo, balance and savings rate."""

import sqlite3
from typing import Any

from coded_tools.finance_agent.finance_store import FinanceTool
from coded_tools.finance_agent.finance_store import contributions_for_month
from coded_tools.finance_agent.finance_store import expenses_for_month
from coded_tools.finance_agent.finance_store import income_for_month
from coded_tools.finance_agent.finance_store import month_bounds
from coded_tools.finance_agent.finance_store import parse_month
from coded_tools.finance_agent.finance_store import to_major

TOP_EXPENSES = 5


class ComputeBalance(FinanceTool):
    """
    Answers "where do I stand this month?" and "how have I been trending?".

    Balance is income minus expenses. Goal contributions are reported alongside rather
    than subtracted, because money moved into a goal is saved, not spent - so the tool
    also reports what is left over after those transfers as `unallocated`.
    """

    def run(self, connection: sqlite3.Connection, args: dict[str, Any], user_id: str) -> Any:
        month = parse_month(args.get("month"))
        result = self._month_view(connection, user_id, month)

        trailing = self._trailing_months(args.get("trailing_months"))
        if trailing > 1:
            history = [
                self._totals(connection, user_id, past_month)
                for past_month in self._recent_months(month, trailing)
            ]
            result["history"] = history
            result["average_monthly_balance"] = round(
                sum(entry["balance"] for entry in history) / len(history), 2
            )
            result["average_monthly_expenses"] = round(
                sum(entry["total_expenses"] for entry in history) / len(history), 2
            )

        return result

    def _month_view(self, connection: sqlite3.Connection, user_id: str, month: str) -> dict[str, Any]:
        """The detailed single-month picture, with breakdowns the agent can narrate."""
        income_rows = income_for_month(connection, user_id, month)
        expense_rows = expenses_for_month(connection, user_id, month)

        income_minor = sum(int(row["amount_minor"]) for row in income_rows)
        expense_minor = sum(int(row["amount_minor"]) for row in expense_rows)
        balance_minor = income_minor - expense_minor
        allocated_minor = contributions_for_month(connection, user_id, month)

        by_category: dict[str, int] = {}
        for row in expense_rows:
            by_category[row["category"]] = by_category.get(row["category"], 0) + int(row["amount_minor"])

        first_day, last_day = month_bounds(month)

        return {
            "month": month,
            "period": {"from": first_day, "to": last_day},
            "total_income": to_major(income_minor),
            "total_expenses": to_major(expense_minor),
            "balance": to_major(balance_minor),
            "savings_rate_percent": round(balance_minor / income_minor * 100, 1) if income_minor else None,
            "allocated_to_goals": to_major(allocated_minor),
            "unallocated": to_major(balance_minor - allocated_minor),
            "overspent": balance_minor < 0,
            "income_sources": [
                {
                    "source": row["source"],
                    "amount": to_major(row["amount_minor"]),
                    "frequency": row["frequency"],
                }
                for row in income_rows
            ],
            "expenses_by_category": [
                {
                    "category": category,
                    "amount": to_major(minor),
                    "percent_of_expenses": round(minor / expense_minor * 100, 1) if expense_minor else 0.0,
                }
                for category, minor in sorted(by_category.items(), key=lambda pair: -pair[1])
            ],
            "largest_expenses": [
                {
                    "id": int(row["id"]),
                    "category": row["category"],
                    "amount": to_major(row["amount_minor"]),
                    "spent_on": row["spent_on"],
                    "description": row["description"],
                    "recurring": bool(row["recurring"]),
                }
                for row in expense_rows[:TOP_EXPENSES]
            ],
            "transaction_count": len(expense_rows),
        }

    @staticmethod
    def _totals(connection: sqlite3.Connection, user_id: str, month: str) -> dict[str, Any]:
        """A one-line summary of a month, cheap enough to run across a trailing window."""
        income_minor = sum(int(row["amount_minor"]) for row in income_for_month(connection, user_id, month))
        expense_minor = sum(int(row["amount_minor"]) for row in expenses_for_month(connection, user_id, month))
        return {
            "month": month,
            "total_income": to_major(income_minor),
            "total_expenses": to_major(expense_minor),
            "balance": to_major(income_minor - expense_minor),
        }

    @staticmethod
    def _trailing_months(raw: Any) -> int:
        """Clamp the history window; 1 means "just this month, no history"."""
        try:
            return max(1, min(24, int(raw)))
        except (TypeError, ValueError):
            return 1

    @staticmethod
    def _recent_months(month: str, count: int) -> list[str]:
        """The `count` months ending at `month`, oldest first."""
        year, number = int(month[:4]), int(month[5:7])
        months: list[str] = []
        for offset in range(count - 1, -1, -1):
            total = year * 12 + (number - 1) - offset
            months.append(f"{total // 12:04d}-{total % 12 + 1:02d}")
        return months
