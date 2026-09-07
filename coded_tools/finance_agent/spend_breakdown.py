"""CodedTool that analyses spending by category, optionally against another month."""

import sqlite3
from typing import Any

from coded_tools.finance_agent.finance_store import FinanceTool
from coded_tools.finance_agent.finance_store import expenses_for_month
from coded_tools.finance_agent.finance_store import income_for_month
from coded_tools.finance_agent.finance_store import normalise_category
from coded_tools.finance_agent.finance_store import parse_month
from coded_tools.finance_agent.finance_store import to_major

# Below this share of total spend a category is noise, not a finding worth reporting.
NOTABLE_SHARE_PERCENT = 5.0


class SpendBreakdown(FinanceTool):
    """
    Breaks a month's spending into categories and puts each one in context.

    Every category is expressed as a share of spend *and* a share of income, because
    "38% of your spend went to rent" and "rent eats 31% of what you earn" answer
    different questions. Pass `compare_to_month` to get month-over-month movement.
    """

    def run(self, connection: sqlite3.Connection, args: dict[str, Any], user_id: str) -> Any:
        month = parse_month(args.get("month"))
        category_filter = normalise_category(args["category"]) if args.get("category") else None

        current = self._breakdown(connection, user_id, month, category_filter)

        raw_comparison = args.get("compare_to_month")
        if raw_comparison:
            baseline_month = parse_month(raw_comparison, "compare_to_month")
            baseline = self._breakdown(connection, user_id, baseline_month, category_filter)
            current["comparison"] = self._compare(current, baseline)

        return current

    @staticmethod
    def _breakdown(
        connection: sqlite3.Connection,
        user_id: str,
        month: str,
        category_filter: str | None,
    ) -> dict[str, Any]:
        """Aggregate one month's expenses into per-category rows."""
        rows = expenses_for_month(connection, user_id, month)
        if category_filter:
            rows = [row for row in rows if row["category"] == category_filter]

        income_minor = sum(int(row["amount_minor"]) for row in income_for_month(connection, user_id, month))
        total_minor = sum(int(row["amount_minor"]) for row in rows)

        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            bucket = grouped.setdefault(
                row["category"], {"amount_minor": 0, "count": 0, "recurring_minor": 0}
            )
            bucket["amount_minor"] += int(row["amount_minor"])
            bucket["count"] += 1
            if row["recurring"]:
                bucket["recurring_minor"] += int(row["amount_minor"])

        categories = [
            {
                "category": name,
                "amount": to_major(bucket["amount_minor"]),
                "transactions": bucket["count"],
                "recurring_amount": to_major(bucket["recurring_minor"]),
                "percent_of_spend": round(bucket["amount_minor"] / total_minor * 100, 1) if total_minor else 0.0,
                "percent_of_income": round(bucket["amount_minor"] / income_minor * 100, 1)
                if income_minor
                else None,
            }
            for name, bucket in sorted(grouped.items(), key=lambda pair: -pair[1]["amount_minor"])
        ]

        fixed_minor = sum(int(row["amount_minor"]) for row in rows if row["recurring"])

        return {
            "month": month,
            "category_filter": category_filter,
            "total_spend": to_major(total_minor),
            "total_income": to_major(income_minor),
            "spend_as_percent_of_income": round(total_minor / income_minor * 100, 1) if income_minor else None,
            "fixed_spend": to_major(fixed_minor),
            "discretionary_spend": to_major(total_minor - fixed_minor),
            "categories": categories,
            "biggest_category": categories[0]["category"] if categories else None,
            "notable_categories": [item["category"] for item in categories if item["percent_of_spend"] >= NOTABLE_SHARE_PERCENT],
        }

    @staticmethod
    def _compare(current: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
        """Diff two breakdowns, surfacing per-category movement and new/dropped categories."""
        baseline_amounts = {item["category"]: item["amount"] for item in baseline["categories"]}
        current_amounts = {item["category"]: item["amount"] for item in current["categories"]}

        changes = []
        for category in sorted(set(baseline_amounts) | set(current_amounts)):
            was = baseline_amounts.get(category, 0.0)
            now = current_amounts.get(category, 0.0)
            changes.append(
                {
                    "category": category,
                    "previous_amount": was,
                    "current_amount": now,
                    "change": round(now - was, 2),
                    "percent_change": round((now - was) / was * 100, 1) if was else None,
                }
            )
        changes.sort(key=lambda item: -abs(item["change"]))

        return {
            "baseline_month": baseline["month"],
            "previous_total_spend": baseline["total_spend"],
            "total_spend_change": round(current["total_spend"] - baseline["total_spend"], 2),
            "by_category": changes,
            "new_categories": sorted(set(current_amounts) - set(baseline_amounts)),
            "dropped_categories": sorted(set(baseline_amounts) - set(current_amounts)),
        }
