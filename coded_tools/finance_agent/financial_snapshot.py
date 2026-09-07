"""CodedTool that assembles the whole financial picture for the insights agent."""

import sqlite3
from typing import Any

from coded_tools.finance_agent.finance_store import FinanceTool
from coded_tools.finance_agent.finance_store import contributions_for_month
from coded_tools.finance_agent.finance_store import expenses_for_month
from coded_tools.finance_agent.finance_store import goal_progress
from coded_tools.finance_agent.finance_store import income_for_month
from coded_tools.finance_agent.finance_store import parse_month
from coded_tools.finance_agent.finance_store import to_major

# Rules of thumb the insights agent narrates against. Deliberately conservative and
# stated as data rather than baked into prose, so the agent can explain the threshold
# it is comparing to instead of asserting a number out of nowhere.
HEALTHY_SAVINGS_RATE = 20.0
FIXED_SPEND_CEILING = 50.0
EMERGENCY_FUND_MONTHS = 6


class FinancialSnapshot(FinanceTool):
    """
    One call that returns income, spend, balance, goals and derived health metrics.

    The insights agent needs all of these together to say anything useful - advising on
    savings rate without knowing goal commitments produces advice that double-counts
    the same rupee. Gathering it in a single tool also keeps the agent from making five
    sequential tool calls just to form one opinion.
    """

    def run(self, connection: sqlite3.Connection, args: dict[str, Any], user_id: str) -> Any:
        month = parse_month(args.get("month"))

        income_rows = income_for_month(connection, user_id, month)
        expense_rows = expenses_for_month(connection, user_id, month)

        income_minor = sum(int(row["amount_minor"]) for row in income_rows)
        expense_minor = sum(int(row["amount_minor"]) for row in expense_rows)
        fixed_minor = sum(int(row["amount_minor"]) for row in expense_rows if row["recurring"])
        balance_minor = income_minor - expense_minor
        allocated_minor = contributions_for_month(connection, user_id, month)

        goal_rows = connection.execute(
            "SELECT * FROM goals WHERE user_id = ? AND status = 'active' ORDER BY id", (user_id,)
        ).fetchall()
        goals = [goal_progress(connection, row) for row in goal_rows]
        committed = round(sum(goal.get("required_monthly_contribution") or 0.0 for goal in goals), 2)

        by_category: dict[str, int] = {}
        for row in expense_rows:
            by_category[row["category"]] = by_category.get(row["category"], 0) + int(row["amount_minor"])

        savings_rate = round(balance_minor / income_minor * 100, 1) if income_minor else None
        fixed_share = round(fixed_minor / income_minor * 100, 1) if income_minor else None

        return {
            "month": month,
            "has_data": bool(income_rows or expense_rows or goal_rows),
            "income": {
                "total": to_major(income_minor),
                "streams": [
                    {"source": row["source"], "amount": to_major(row["amount_minor"]), "frequency": row["frequency"]}
                    for row in income_rows
                ],
            },
            "spending": {
                "total": to_major(expense_minor),
                "fixed": to_major(fixed_minor),
                "discretionary": to_major(expense_minor - fixed_minor),
                "by_category": [
                    {"category": name, "amount": to_major(minor)}
                    for name, minor in sorted(by_category.items(), key=lambda pair: -pair[1])
                ],
            },
            "balance": {
                "surplus": to_major(balance_minor),
                "allocated_to_goals": to_major(allocated_minor),
                "unallocated": to_major(balance_minor - allocated_minor),
                "overspent": balance_minor < 0,
            },
            "goals": goals,
            "goal_commitment_per_month": committed,
            "health": {
                "savings_rate_percent": savings_rate,
                "healthy_savings_rate_percent": HEALTHY_SAVINGS_RATE,
                "savings_rate_is_healthy": savings_rate is not None and savings_rate >= HEALTHY_SAVINGS_RATE,
                "fixed_spend_percent_of_income": fixed_share,
                "fixed_spend_ceiling_percent": FIXED_SPEND_CEILING,
                "fixed_spend_is_heavy": fixed_share is not None and fixed_share > FIXED_SPEND_CEILING,
                "emergency_fund_target_months": EMERGENCY_FUND_MONTHS,
                "emergency_fund_target_amount": to_major(expense_minor * EMERGENCY_FUND_MONTHS),
                "goal_plan_affordable": committed <= to_major(balance_minor),
                "goal_plan_shortfall": round(max(0.0, committed - to_major(balance_minor)), 2),
            },
        }
