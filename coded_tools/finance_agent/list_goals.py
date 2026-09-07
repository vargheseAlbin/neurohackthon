"""CodedTool that reports goal progress and whether the current surplus can sustain it."""

import math
import sqlite3
from typing import Any

from coded_tools.finance_agent.finance_store import GOAL_STATUSES
from coded_tools.finance_agent.finance_store import FinanceTool
from coded_tools.finance_agent.finance_store import contributions_for_month
from coded_tools.finance_agent.finance_store import expenses_for_month
from coded_tools.finance_agent.finance_store import find_goal
from coded_tools.finance_agent.finance_store import goal_progress
from coded_tools.finance_agent.finance_store import income_for_month
from coded_tools.finance_agent.finance_store import parse_choice
from coded_tools.finance_agent.finance_store import parse_month
from coded_tools.finance_agent.finance_store import to_major

RECENT_CONTRIBUTIONS = 10


class ListGoals(FinanceTool):
    """
    The read side of goal tracking.

    Progress alone is not advice, so every goal is also checked against the monthly
    surplus the ledger actually shows: what pace the deadline demands, what pace the
    user can afford, and how long the goal takes at the affordable pace. When the
    demanded pace exceeds the available surplus the goal is flagged rather than
    presented as fine.
    """

    def run(self, connection: sqlite3.Connection, args: dict[str, Any], user_id: str) -> Any:
        if args.get("goal"):
            return self._single(connection, args, user_id)

        month = parse_month(args.get("month"))
        status_filter = (
            parse_choice(args["status"], GOAL_STATUSES, "status", "active") if args.get("status") else None
        )

        query = "SELECT * FROM goals WHERE user_id = ?"
        parameters: list[Any] = [user_id]
        if status_filter:
            query += " AND status = ?"
            parameters.append(status_filter)
        query += " ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, id"

        rows = connection.execute(query, parameters).fetchall()
        if not rows:
            return {
                "goals": [],
                "note": "No goals recorded yet."
                if not status_filter
                else f"No goals with status '{status_filter}'.",
            }

        surplus = self._monthly_surplus(connection, user_id, month)
        goals = [self._with_feasibility(connection, row, surplus) for row in rows]

        required_total = sum(goal.get("required_monthly_contribution") or 0.0 for goal in goals)

        return {
            "as_of_month": month,
            "monthly_surplus": surplus["surplus"],
            "surplus_basis": surplus,
            "goals": goals,
            "total_target": round(sum(goal["target_amount"] for goal in goals), 2),
            "total_saved": round(sum(goal["saved_amount"] for goal in goals), 2),
            "total_required_monthly": round(required_total, 2),
            "plan_is_affordable": required_total <= surplus["surplus"],
            "monthly_shortfall": round(max(0.0, required_total - surplus["surplus"]), 2),
            "at_risk_goals": [goal["name"] for goal in goals if goal.get("at_risk")],
        }

    def _single(self, connection: sqlite3.Connection, args: dict[str, Any], user_id: str) -> dict[str, Any]:
        """One goal in full, including its contribution history."""
        row = find_goal(connection, user_id, args["goal"])
        month = parse_month(args.get("month"))
        surplus = self._monthly_surplus(connection, user_id, month)

        detail = self._with_feasibility(connection, row, surplus)
        history = connection.execute(
            """
            SELECT amount_minor, contributed_on, note
              FROM goal_contributions
             WHERE goal_id = ?
             ORDER BY contributed_on DESC, id DESC
             LIMIT ?
            """,
            (int(row["id"]), RECENT_CONTRIBUTIONS),
        ).fetchall()

        detail["recent_contributions"] = [
            {
                "amount": to_major(entry["amount_minor"]),
                "on": entry["contributed_on"],
                "note": entry["note"],
            }
            for entry in history
        ]
        detail["as_of_month"] = month
        detail["monthly_surplus"] = surplus["surplus"]
        return detail

    @staticmethod
    def _with_feasibility(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        surplus: dict[str, Any],
    ) -> dict[str, Any]:
        """Attach affordability signals to a goal's raw progress."""
        progress = goal_progress(connection, row)
        remaining = progress["remaining_amount"]
        available = surplus["surplus"]

        if remaining <= 0:
            progress["at_risk"] = False
            progress["months_at_current_surplus"] = 0
            return progress

        if available > 0:
            progress["months_at_current_surplus"] = math.ceil(remaining / available)
        else:
            # No surplus means no arrival date, and saying "0 months" would be a lie.
            progress["months_at_current_surplus"] = None
            progress["blocked_reason"] = "No monthly surplus available to save from."

        required = progress.get("required_monthly_contribution")
        if required is not None:
            progress["surplus_covers_required_pace"] = required <= available
            progress["at_risk"] = required > available
        else:
            # No deadline set, so nothing to fall behind on - only reachability matters.
            progress["at_risk"] = available <= 0

        return progress

    @staticmethod
    def _monthly_surplus(connection: sqlite3.Connection, user_id: str, month: str) -> dict[str, Any]:
        """
        What is genuinely available to save this month.

        Contributions already made this month are added back, otherwise funding a goal
        would shrink the surplus used to judge whether that same goal is affordable.
        """
        income_minor = sum(int(entry["amount_minor"]) for entry in income_for_month(connection, user_id, month))
        expense_minor = sum(
            int(entry["amount_minor"]) for entry in expenses_for_month(connection, user_id, month)
        )
        already_minor = contributions_for_month(connection, user_id, month)
        surplus_minor = income_minor - expense_minor

        return {
            "month": month,
            "total_income": to_major(income_minor),
            "total_expenses": to_major(expense_minor),
            "surplus": to_major(surplus_minor),
            "already_contributed_this_month": to_major(already_minor),
            "still_unallocated": to_major(surplus_minor - already_minor),
        }
