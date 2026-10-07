"""Offline, deliberately small DDL templates. No execution path."""
import re


def identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", value):
        raise ValueError("Unsafe SQL identifier")
    return f"[{value}]"


def propose(kind: str, schema: str, table: str, name: str, data_type: str = "int") -> dict:
    target = f"{identifier(schema)}.{identifier(table)}"
    field = identifier(name)
    if kind == "add_nullable_column":
        if data_type not in ("int", "bigint", "bit", "datetime2(7)", "nvarchar(255)"):
            raise ValueError("Unsupported type")
        sql = f"ALTER TABLE {target} ADD {field} {data_type} NULL;"
        rollback = f"ALTER TABLE {target} DROP COLUMN {field};"
    elif kind == "create_index":
        index = identifier(f"IX_{table}_{name}")
        sql = f"CREATE NONCLUSTERED INDEX {index} ON {target} ({field});"
        rollback = f"DROP INDEX {index} ON {target};"
    else:
        raise ValueError("Unsupported proposal")
    return {"status": "PROPOSAL ONLY — never executed", "sql": sql,
            "rollback_proposal": rollback,
            "required_review": ["Bind to exact tenant/server/database in change ticket",
                                "Verify object existence and current schema",
                                "Validate application compatibility and dependent objects",
                                "Measure locks, duration, storage and workload impact in staging",
                                "Verify backup and restore evidence; set maintenance window",
                                "Obtain separate explicit production write approval"],
            "rollback_warning": "Dropping columns may destroy data; removing indexes changes plans."}
