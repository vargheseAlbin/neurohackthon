"""CodedTool that tells the agents what "today", "this month" and "last month" mean."""

import sqlite3
from datetime import date
from datetime import timedelta
from typing import Any

from coded_tools.finance_agent.finance_store import FinanceTool


class CurrentPeriod(FinanceTool):
    """
    Resolves relative dates to the exact strings the other finance tools expect.

    An LLM has no reliable clock, so without this it either invents a date or asks the
    user what day it is. This returns local dates - matching what the ledger records -
    rather than UTC, so "today" does not drift a day either side of midnight.
    """

    def run(self, connection: sqlite3.Connection, args: dict[str, Any], user_id: str) -> Any:
        today = date.today()
        first_of_month = today.replace(day=1)
        last_month_end = first_of_month - timedelta(days=1)

        return {
            "today": today.isoformat(),
            "yesterday": (today - timedelta(days=1)).isoformat(),
            "this_month": today.strftime("%Y-%m"),
            "this_month_name": today.strftime("%B %Y"),
            "last_month": last_month_end.strftime("%Y-%m"),
            "last_month_name": last_month_end.strftime("%B %Y"),
            "day_of_month": today.day,
            "days_left_in_month": (
                (first_of_month.replace(year=today.year + 1, month=1) if today.month == 12
                 else first_of_month.replace(month=today.month + 1)) - today
            ).days,
        }
