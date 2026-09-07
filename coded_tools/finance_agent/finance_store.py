"""
Shared persistence layer for the finance_agent network.

Everything the sub-agents record - income, expenses, goals and goal contributions -
lands in a single SQLite file so that state survives across conversations. Amounts are
stored as integer minor units (paise/cents) because floats accumulate rounding error
once you start summing a year of transactions.

Tools in this package subclass FinanceTool and implement run(); the base class owns
the connection, the user scoping and the error-to-string conversion that the calling
agent relays to the user.
"""

import asyncio
import math
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any
from typing import Iterator

from neuro_san.interfaces.coded_tool import CodedTool

# coded_tools/finance_agent/finance_store.py -> repo root is three levels up.
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = REPO_ROOT / "finance_local.db"

# Single currency per deployment. This app tracks one person's money, so a
# multi-currency ledger would be complexity with no payoff.
CURRENCY = os.getenv("FINANCE_CURRENCY", "INR")

FREQUENCIES = ("monthly", "one_time")
PRIORITIES = ("high", "medium", "low")
GOAL_STATUSES = ("active", "achieved", "paused", "abandoned")

# Multipliers for the shorthand people actually type at a chat prompt.
MAGNITUDE_SUFFIXES: dict[str, int] = {
    "k": 1_000,
    "thousand": 1_000,
    "l": 100_000,
    "lac": 100_000,
    "lakh": 100_000,
    "lakhs": 100_000,
    "m": 1_000_000,
    "million": 1_000_000,
    "cr": 10_000_000,
    "crore": 10_000_000,
    "crores": 10_000_000,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS income (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       TEXT    NOT NULL,
    source        TEXT    NOT NULL,
    amount_minor  INTEGER NOT NULL,
    frequency     TEXT    NOT NULL DEFAULT 'monthly',
    starts_on     TEXT    NOT NULL,
    active        INTEGER NOT NULL DEFAULT 1,
    notes         TEXT,
    created_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS expenses (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       TEXT    NOT NULL,
    category      TEXT    NOT NULL,
    amount_minor  INTEGER NOT NULL,
    spent_on      TEXT    NOT NULL,
    description   TEXT,
    recurring     INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS goals (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id                     TEXT    NOT NULL,
    name                        TEXT    NOT NULL,
    target_minor                INTEGER NOT NULL,
    target_date                 TEXT,
    priority                    TEXT    NOT NULL DEFAULT 'medium',
    status                      TEXT    NOT NULL DEFAULT 'active',
    monthly_contribution_minor  INTEGER,
    notes                       TEXT,
    created_at                  TEXT    NOT NULL,
    UNIQUE (user_id, name)
);

CREATE TABLE IF NOT EXISTS goal_contributions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id         INTEGER NOT NULL REFERENCES goals (id) ON DELETE CASCADE,
    amount_minor    INTEGER NOT NULL,
    contributed_on  TEXT    NOT NULL,
    note            TEXT,
    created_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_expenses_user_date ON expenses (user_id, spent_on);
CREATE INDEX IF NOT EXISTS idx_income_user_date   ON income   (user_id, starts_on);
CREATE INDEX IF NOT EXISTS idx_goals_user         ON goals    (user_id);
CREATE INDEX IF NOT EXISTS idx_contrib_goal       ON goal_contributions (goal_id);
"""


class FinanceError(Exception):
    """A bad argument from the calling agent, phrased so the user can act on it."""


# --------------------------------------------------------------------------------------
# Connection handling
# --------------------------------------------------------------------------------------


@contextmanager
def open_ledger() -> Iterator[sqlite3.Connection]:
    """Yield a connection with the schema applied, committing on clean exit."""
    db_path = os.getenv("FINANCE_DB_PATH") or str(DEFAULT_DB_PATH)
    connection = sqlite3.connect(db_path, timeout=15.0)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(SCHEMA)
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def user_of(sly_data: dict[str, Any]) -> str:
    """
    Scope every row to a user id taken from sly_data, never from the chat stream.

    A single-user desktop run just gets "default"; a hosted deployment sets
    sly_data["user_id"] per conversation and the ledgers stay separate.
    """
    return str(sly_data.get("user_id") or "default")


# --------------------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------------------


def to_minor(raw: Any, field: str = "amount") -> int:
    """
    Convert a user-or-LLM-supplied amount into integer minor units.

    Accepts plain numbers plus the shorthand that shows up in chat: "45,000",
    "Rs 45000", "1.2 lakh", "$1.5k". Rejects anything negative - a refund is an
    income entry, not a negative expense, and letting sign through here would
    quietly corrupt every downstream sum.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raise FinanceError(f"'{field}' is required.")

    if isinstance(raw, bool):
        raise FinanceError(f"'{field}' must be a number, not a true/false value.")

    if isinstance(raw, (int, float)):
        value = float(raw)
    else:
        value = _parse_amount_text(str(raw), field)

    if not math.isfinite(value):
        raise FinanceError(f"'{field}' must be a finite number.")
    if value < 0:
        raise FinanceError(f"'{field}' cannot be negative. Record money coming in as income instead.")

    # round() rather than int() so 0.005 -> 1 paisa instead of being truncated away.
    return int(round(value * 100))


def _parse_amount_text(text: str, field: str) -> float:
    """Pull a number and optional magnitude suffix out of free-form amount text."""
    cleaned = text.strip().lower()
    # Drop currency symbols, codes, commas and spaces; keep digits, dot and letters.
    cleaned = re.sub(r"(?:rs\.?|inr|usd|eur|gbp|[₹$€£,\s_])", "", cleaned)

    match = re.fullmatch(r"(\d+(?:\.\d+)?)([a-z]*)", cleaned)
    if not match:
        raise FinanceError(f"Could not read '{text}' as an {field}. Try a plain number like 45000.")

    number = float(match.group(1))
    suffix = match.group(2)
    if not suffix:
        return number
    if suffix not in MAGNITUDE_SUFFIXES:
        raise FinanceError(f"Unknown amount unit '{suffix}' in '{text}'. Use a plain number like 45000.")
    return number * MAGNITUDE_SUFFIXES[suffix]


def to_major(minor: int | None) -> float:
    """Convert stored minor units back to a display amount."""
    return round((minor or 0) / 100, 2)


def parse_day(raw: Any, field: str = "date", default: date | None = None) -> str:
    """Normalise a YYYY-MM-DD date, defaulting to today when omitted."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return (default or date.today()).isoformat()

    text = str(raw).strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as error:
        raise FinanceError(f"'{field}' must be a date like 2026-09-07, got '{text}'.") from error


def parse_month(raw: Any, field: str = "month") -> str:
    """
    Normalise a YYYY-MM month key, defaulting to the current month.

    A full date is accepted too and truncated, because agents routinely pass
    "2026-09-01" when they mean September.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return date.today().strftime("%Y-%m")

    text = str(raw).strip()
    if re.fullmatch(r"\d{4}-\d{2}", text):
        month_number = int(text[5:7])
        if not 1 <= month_number <= 12:
            raise FinanceError(f"'{field}' has an impossible month: '{text}'.")
        return text
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return parse_day(text, field)[:7]
    raise FinanceError(f"'{field}' must look like 2026-09, got '{text}'.")


def parse_choice(raw: Any, allowed: tuple[str, ...], field: str, default: str) -> str:
    """Validate a small enum-ish argument, defaulting when the agent omits it."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    value = str(raw).strip().lower().replace("-", "_").replace(" ", "_")
    if value not in allowed:
        raise FinanceError(f"'{field}' must be one of {', '.join(allowed)}, got '{raw}'.")
    return value


def normalise_category(raw: Any) -> str:
    """Lower-case and squash whitespace so 'Eating Out' and 'eating  out' aggregate together."""
    text = re.sub(r"\s+", " ", str(raw or "").strip()).lower()
    if not text:
        raise FinanceError("'category' is required, e.g. rent, groceries, transport, dining.")
    return text


def months_between(start_month: str, end_month: str) -> int:
    """Whole months from start_month to end_month, both 'YYYY-MM'. Negative if reversed."""
    start_year, start_num = int(start_month[:4]), int(start_month[5:7])
    end_year, end_num = int(end_month[:4]), int(end_month[5:7])
    return (end_year - start_year) * 12 + (end_num - start_num)


def month_bounds(month: str) -> tuple[str, str]:
    """Return the inclusive first and last ISO dates of a 'YYYY-MM' month."""
    year, number = int(month[:4]), int(month[5:7])
    first = date(year, number, 1)
    last_year, last_number = (year + 1, 1) if number == 12 else (year, number + 1)
    last = date(last_year, last_number, 1).toordinal() - 1
    return first.isoformat(), date.fromordinal(last).isoformat()


# --------------------------------------------------------------------------------------
# Query helpers shared by several tools
# --------------------------------------------------------------------------------------


def income_for_month(connection: sqlite3.Connection, user_id: str, month: str) -> list[sqlite3.Row]:
    """
    Income streams that pay out in the given month.

    A 'monthly' stream counts in every month from the one it started in onwards, so a
    salary recorded once keeps showing up. A 'one_time' entry counts only in its own month.
    """
    return connection.execute(
        """
        SELECT * FROM income
         WHERE user_id = ?
           AND active = 1
           AND (
                 (frequency = 'monthly'  AND substr(starts_on, 1, 7) <= ?)
              OR (frequency = 'one_time' AND substr(starts_on, 1, 7)  = ?)
           )
         ORDER BY amount_minor DESC
        """,
        (user_id, month, month),
    ).fetchall()


def expenses_for_month(connection: sqlite3.Connection, user_id: str, month: str) -> list[sqlite3.Row]:
    """Expenses hitting the given month, expanding recurring ones the same way as income."""
    return connection.execute(
        """
        SELECT * FROM expenses
         WHERE user_id = ?
           AND (
                 (recurring = 1 AND substr(spent_on, 1, 7) <= ?)
              OR (recurring = 0 AND substr(spent_on, 1, 7)  = ?)
           )
         ORDER BY amount_minor DESC
        """,
        (user_id, month, month),
    ).fetchall()


def contributions_for_month(connection: sqlite3.Connection, user_id: str, month: str) -> int:
    """Total minor units moved into goals during the given month."""
    row = connection.execute(
        """
        SELECT COALESCE(SUM(c.amount_minor), 0) AS total
          FROM goal_contributions c
          JOIN goals g ON g.id = c.goal_id
         WHERE g.user_id = ?
           AND substr(c.contributed_on, 1, 7) = ?
        """,
        (user_id, month),
    ).fetchone()
    return int(row["total"])


def saved_minor(connection: sqlite3.Connection, goal_id: int) -> int:
    """Total minor units contributed to one goal over its lifetime."""
    row = connection.execute(
        "SELECT COALESCE(SUM(amount_minor), 0) AS total FROM goal_contributions WHERE goal_id = ?",
        (goal_id,),
    ).fetchone()
    return int(row["total"])


def find_goal(connection: sqlite3.Connection, user_id: str, identifier: Any) -> sqlite3.Row:
    """
    Look a goal up by numeric id or by name, tolerating case and spacing differences.

    Raises FinanceError with the available goal names when nothing matches, which gives
    the calling agent something concrete to say instead of "not found".
    """
    text = str(identifier or "").strip()
    if not text:
        raise FinanceError("Which goal? Pass a goal name or id.")

    if text.isdigit():
        row = connection.execute(
            "SELECT * FROM goals WHERE user_id = ? AND id = ?", (user_id, int(text))
        ).fetchone()
        if row is not None:
            return row

    row = connection.execute(
        "SELECT * FROM goals WHERE user_id = ? AND lower(trim(name)) = ?",
        (user_id, text.lower()),
    ).fetchone()
    if row is not None:
        return row

    # Last resort: a unique substring match, so "emergency" finds "Emergency fund".
    candidates = connection.execute(
        "SELECT * FROM goals WHERE user_id = ? AND lower(name) LIKE ?",
        (user_id, f"%{text.lower()}%"),
    ).fetchall()
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        names = ", ".join(row["name"] for row in candidates)
        raise FinanceError(f"'{text}' matches several goals: {names}. Which one?")

    existing = connection.execute(
        "SELECT name FROM goals WHERE user_id = ? ORDER BY name", (user_id,)
    ).fetchall()
    if not existing:
        raise FinanceError("There are no goals yet. Create one first.")
    raise FinanceError(f"No goal called '{text}'. Existing goals: {', '.join(r['name'] for r in existing)}.")


def goal_progress(connection: sqlite3.Connection, row: sqlite3.Row, today: date | None = None) -> dict[str, Any]:
    """Build the progress view of one goal: saved, remaining, pace needed, and slack."""
    today = today or date.today()
    target = int(row["target_minor"])
    saved = saved_minor(connection, int(row["id"]))
    remaining = max(0, target - saved)

    progress: dict[str, Any] = {
        "id": int(row["id"]),
        "name": row["name"],
        "status": row["status"],
        "priority": row["priority"],
        "target_amount": to_major(target),
        "saved_amount": to_major(saved),
        "remaining_amount": to_major(remaining),
        "progress_percent": round(min(100.0, saved / target * 100), 1) if target else 0.0,
        "target_date": row["target_date"],
        "planned_monthly_contribution": to_major(row["monthly_contribution_minor"])
        if row["monthly_contribution_minor"]
        else None,
        "notes": row["notes"],
    }

    if row["target_date"]:
        months_left = months_between(today.strftime("%Y-%m"), str(row["target_date"])[:7])
        progress["months_remaining"] = months_left
        if remaining == 0:
            progress["required_monthly_contribution"] = 0.0
        elif months_left > 0:
            progress["required_monthly_contribution"] = to_major(math.ceil(remaining / months_left))
        else:
            # Deadline is this month or already gone - the whole remainder is due now.
            progress["required_monthly_contribution"] = to_major(remaining)
            progress["deadline_passed"] = months_left < 0

    return progress


# --------------------------------------------------------------------------------------
# Base class for the tools in this package
# --------------------------------------------------------------------------------------


class FinanceTool(CodedTool):
    """
    Base for the finance CodedTools: opens the ledger, scopes to a user, formats errors.

    SQLite calls are synchronous, so run() is handed to a worker thread rather than
    blocking the event loop that every other agent in the network shares.
    """

    def invoke(self, args: dict[str, Any], sly_data: dict[str, Any]) -> Any:
        try:
            with open_ledger() as connection:
                result = self.run(connection, args, user_of(sly_data))
        except FinanceError as error:
            return f"Error: {error}"
        except sqlite3.Error as error:
            return f"Error: the finance ledger could not be updated ({error})."

        if isinstance(result, dict):
            result.setdefault("currency", CURRENCY)
        return result

    async def async_invoke(self, args: dict[str, Any], sly_data: dict[str, Any]) -> Any:
        return await asyncio.to_thread(self.invoke, args, sly_data)

    def run(self, connection: sqlite3.Connection, args: dict[str, Any], user_id: str) -> Any:
        """Do the tool's actual work against an open, schema-applied connection."""
        raise NotImplementedError
