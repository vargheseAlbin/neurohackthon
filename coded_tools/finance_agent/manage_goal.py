"""CodedTool that creates goals and moves money into them."""

import sqlite3
from datetime import datetime
from typing import Any

from coded_tools.finance_agent.finance_store import GOAL_STATUSES
from coded_tools.finance_agent.finance_store import PRIORITIES
from coded_tools.finance_agent.finance_store import FinanceError
from coded_tools.finance_agent.finance_store import FinanceTool
from coded_tools.finance_agent.finance_store import find_goal
from coded_tools.finance_agent.finance_store import goal_progress
from coded_tools.finance_agent.finance_store import parse_choice
from coded_tools.finance_agent.finance_store import parse_day
from coded_tools.finance_agent.finance_store import saved_minor
from coded_tools.finance_agent.finance_store import to_major
from coded_tools.finance_agent.finance_store import to_minor

ACTIONS = ("create", "update", "contribute", "withdraw", "complete", "delete")


class ManageGoal(FinanceTool):
    """
    The write side of goal tracking: create, retarget, fund and close out goals.

    Contributions are stored as individual dated rows rather than a running total on the
    goal, so "how much did I put toward the house this quarter?" stays answerable and a
    mistaken transfer can be reversed without corrupting the goal's target.
    """

    def run(self, connection: sqlite3.Connection, args: dict[str, Any], user_id: str) -> Any:
        action = parse_choice(args.get("action"), ACTIONS, "action", "create")

        if action == "create":
            return self._create(connection, args, user_id)
        if action == "update":
            return self._update(connection, args, user_id)
        if action in ("contribute", "withdraw"):
            return self._move_money(connection, args, user_id, withdraw=(action == "withdraw"))
        if action == "complete":
            return self._complete(connection, args, user_id)
        return self._delete(connection, args, user_id)

    def _create(self, connection: sqlite3.Connection, args: dict[str, Any], user_id: str) -> dict[str, Any]:
        name = str(args.get("name") or "").strip()
        if not name:
            raise FinanceError("'name' is required, e.g. 'Emergency fund' or 'Japan trip'.")

        target_minor = to_minor(args.get("target_amount"), "target_amount")
        if target_minor <= 0:
            raise FinanceError("'target_amount' must be greater than zero.")

        clash = connection.execute(
            "SELECT id FROM goals WHERE user_id = ? AND lower(trim(name)) = ?",
            (user_id, name.lower()),
        ).fetchone()
        if clash is not None:
            raise FinanceError(f"A goal called '{name}' already exists. Use action 'update' to change it.")

        target_date = parse_day(args["target_date"], "target_date") if args.get("target_date") else None
        priority = parse_choice(args.get("priority"), PRIORITIES, "priority", "medium")
        monthly_minor = (
            to_minor(args["monthly_contribution"], "monthly_contribution")
            if args.get("monthly_contribution") is not None
            else None
        )

        cursor = connection.execute(
            """
            INSERT INTO goals (user_id, name, target_minor, target_date, priority, status,
                               monthly_contribution_minor, notes, created_at)
                 VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)
            """,
            (
                user_id,
                name,
                target_minor,
                target_date,
                priority,
                monthly_minor,
                (str(args["notes"]).strip() if args.get("notes") else None),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )

        # An opening balance saves a second call when the user already has money set aside.
        if args.get("already_saved") is not None:
            self._insert_contribution(
                connection,
                int(cursor.lastrowid),
                to_minor(args["already_saved"], "already_saved"),
                parse_day(None),
                "opening balance",
            )

        row = connection.execute("SELECT * FROM goals WHERE id = ?", (int(cursor.lastrowid),)).fetchone()
        return {"created": "goal", **goal_progress(connection, row)}

    def _update(self, connection: sqlite3.Connection, args: dict[str, Any], user_id: str) -> dict[str, Any]:
        row = find_goal(connection, user_id, args.get("goal") or args.get("name"))

        fields: dict[str, Any] = {}
        if args.get("target_amount") is not None:
            target_minor = to_minor(args["target_amount"], "target_amount")
            if target_minor <= 0:
                raise FinanceError("'target_amount' must be greater than zero.")
            fields["target_minor"] = target_minor
        if args.get("target_date"):
            fields["target_date"] = parse_day(args["target_date"], "target_date")
        if args.get("priority"):
            fields["priority"] = parse_choice(args["priority"], PRIORITIES, "priority", "medium")
        if args.get("status"):
            fields["status"] = parse_choice(args["status"], GOAL_STATUSES, "status", "active")
        if args.get("monthly_contribution") is not None:
            fields["monthly_contribution_minor"] = to_minor(args["monthly_contribution"], "monthly_contribution")
        if args.get("new_name"):
            fields["name"] = str(args["new_name"]).strip()
        if args.get("notes"):
            fields["notes"] = str(args["notes"]).strip()

        if not fields:
            raise FinanceError(
                "Nothing to update. Pass 'target_amount', 'target_date', 'priority', "
                "'status', 'monthly_contribution', 'new_name' or 'notes'."
            )

        assignments = ", ".join(f"{column} = ?" for column in fields)
        connection.execute(
            f"UPDATE goals SET {assignments} WHERE id = ?",
            (*fields.values(), int(row["id"])),
        )

        updated = connection.execute("SELECT * FROM goals WHERE id = ?", (int(row["id"]),)).fetchone()
        return {"updated": "goal", "changed_fields": sorted(fields), **goal_progress(connection, updated)}

    def _move_money(
        self,
        connection: sqlite3.Connection,
        args: dict[str, Any],
        user_id: str,
        withdraw: bool,
    ) -> dict[str, Any]:
        row = find_goal(connection, user_id, args.get("goal") or args.get("name"))
        amount_minor = to_minor(args.get("amount"))
        if amount_minor <= 0:
            raise FinanceError("'amount' must be greater than zero.")

        goal_id = int(row["id"])
        if withdraw:
            available = saved_minor(connection, goal_id)
            if amount_minor > available:
                raise FinanceError(
                    f"Only {to_major(available)} is saved toward '{row['name']}'; "
                    f"cannot withdraw {to_major(amount_minor)}."
                )
            amount_minor = -amount_minor

        self._insert_contribution(
            connection,
            goal_id,
            amount_minor,
            parse_day(args.get("contributed_on"), "contributed_on"),
            (str(args["note"]).strip() if args.get("note") else None),
        )

        progress = goal_progress(connection, row)
        reached = progress["remaining_amount"] == 0

        # Closing the goal here means the user is told "done" the moment it happens,
        # rather than on some later read of the goal list. A withdrawal that drops the
        # balance back below target reopens it, so it does not sit falsely "achieved".
        if reached and row["status"] == "active":
            connection.execute("UPDATE goals SET status = 'achieved' WHERE id = ?", (goal_id,))
            progress["status"] = "achieved"
        elif not reached and row["status"] == "achieved":
            connection.execute("UPDATE goals SET status = 'active' WHERE id = ?", (goal_id,))
            progress["status"] = "active"
            progress["reopened"] = True

        return {
            "withdrawn" if withdraw else "contributed": to_major(abs(amount_minor)),
            "goal_reached": reached,
            **progress,
        }

    @staticmethod
    def _insert_contribution(
        connection: sqlite3.Connection,
        goal_id: int,
        amount_minor: int,
        contributed_on: str,
        note: str | None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO goal_contributions (goal_id, amount_minor, contributed_on, note, created_at)
                 VALUES (?, ?, ?, ?, ?)
            """,
            (goal_id, amount_minor, contributed_on, note, datetime.now().isoformat(timespec="seconds")),
        )

    @staticmethod
    def _complete(connection: sqlite3.Connection, args: dict[str, Any], user_id: str) -> dict[str, Any]:
        row = find_goal(connection, user_id, args.get("goal") or args.get("name"))
        connection.execute("UPDATE goals SET status = 'achieved' WHERE id = ?", (int(row["id"]),))
        return {"completed": "goal", "id": int(row["id"]), "name": row["name"], "status": "achieved"}

    @staticmethod
    def _delete(connection: sqlite3.Connection, args: dict[str, Any], user_id: str) -> dict[str, Any]:
        row = find_goal(connection, user_id, args.get("goal") or args.get("name"))
        saved = saved_minor(connection, int(row["id"]))
        connection.execute("DELETE FROM goals WHERE id = ?", (int(row["id"]),))
        return {
            "deleted": "goal",
            "id": int(row["id"]),
            "name": row["name"],
            "released_amount": to_major(saved),
            "note": "Contributions logged against this goal were removed with it.",
        }
