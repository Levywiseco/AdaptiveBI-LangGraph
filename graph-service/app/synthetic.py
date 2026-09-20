"""Bounded SQLite fixture, not a production database adapter or SQL firewall."""

import sqlite3
from time import monotonic

import sqlglot
from sqlglot import exp

from app.contracts import Principal, QueryResult

ROWS = [
    (1, "2026-07", "east", 100, 10),
    (2, "2026-07", "west", 200, 0),
    (3, "2026-08", "east", 300, 30),
    (4, "2026-08", "west", 400, 40),
    (5, "2026-08", "east", 500, 0),
]
SCHEMA = (
    "sales(id INTEGER, month TEXT, region TEXT, gross INTEGER, refund INTEGER); net = gross - refund. "
    "month stores YYYY-MM text, for example '2026-08' (August 2026), not full dates. "
    "region stores 'east' (East / 东部) or 'west' (West / 西部)."
)


class SyntheticTools:
    def authorize(self, principal: Principal, datasource_id: str) -> None:
        if (principal.workspace_id != "demo" or datasource_id != "synthetic-sales"
                or datasource_id not in principal.datasource_ids):
            raise PermissionError("datasource_access_denied")

    def schema(self, principal: Principal, datasource_id: str) -> str:
        self.authorize(principal, datasource_id)
        return SCHEMA

    def validate(self, sql: str) -> None:
        try:
            statements = sqlglot.parse(sql, read="sqlite")
        except sqlglot.errors.ParseError as exc:
            raise ValueError("invalid_sql") from exc
        if len(statements) != 1 or not isinstance(statements[0], exp.Select):
            raise ValueError("single_select_required")
        tree = statements[0]
        # Intentionally narrow first milestone: no CTE, subqueries or joins.
        if any(tree.find(kind) for kind in (exp.With, exp.Subquery, exp.Join, exp.Into)):
            raise ValueError("query_shape_not_supported")
        tables = list(tree.find_all(exp.Table))
        if len(tables) != 1 or tables[0].name.lower() != "sales" or tables[0].db or tables[0].catalog:
            raise ValueError("table_not_allowed")
        fields = {"id", "month", "region", "gross", "refund"}
        aliases = {node.alias.lower() for node in tree.find_all(exp.Alias)}
        if any(col.name.lower() not in fields | aliases for col in tree.find_all(exp.Column)):
            raise ValueError("field_not_allowed")

    def execute(self, principal: Principal, datasource_id: str, sql: str) -> QueryResult:
        self.authorize(principal, datasource_id)
        self.validate(sql)
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("CREATE TABLE sales(id INTEGER, month TEXT, region TEXT, gross INTEGER, refund INTEGER)")
            connection.executemany("INSERT INTO sales VALUES (?, ?, ?, ?, ?)", ROWS)
            connection.commit()
            connection.execute("PRAGMA query_only=ON")
            allowed_functions = {"sum", "count", "avg", "min", "max", "round", "coalesce", "abs"}

            def authorizer(action, arg1, arg2, database, trigger):
                if action == sqlite3.SQLITE_SELECT:
                    return sqlite3.SQLITE_OK
                if action == sqlite3.SQLITE_READ and arg1 == "sales":
                    return sqlite3.SQLITE_OK
                if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in allowed_functions:
                    return sqlite3.SQLITE_OK
                return sqlite3.SQLITE_DENY

            connection.set_authorizer(authorizer)
            deadline = monotonic() + 1.0
            connection.set_progress_handler(lambda: int(monotonic() > deadline), 1000)
            cursor = connection.execute(sql)
            rows = cursor.fetchmany(101)
            return QueryResult(columns=[col[0] for col in cursor.description],
                               rows=[list(row) for row in rows[:100]], truncated=len(rows) > 100)
        finally:
            connection.close()
