"""Repeatable CSV import for Azure pokerdb's dbo.BotAccount.

Uses the SqlConnectionString environment variable. Never prints credentials.
Without --apply, reports the exact change counts and rolls back.
"""
from __future__ import annotations

import argparse
import csv
import os
from pathlib import Path


# Matches StrategyUtil.GetProfileForID(1..8) in DecisionMaking.
PROFILE_IDS = {
    "Maniac": 1,
    "Agro Fish": 2,
    "Calling Station": 3,
    "Passive Fish": 4,
    "Strong Balanced": 5,
    "Strong Tight": 6,
    "Strong Loose": 7,
    "Strong Mix": 8,
}


def read_profiles(path: Path) -> dict[str, tuple[str, int]]:
    rows: dict[str, tuple[str, int]] = {}
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None or [x.strip().lower() for x in reader.fieldnames] != ["nickname", "profile"]:
            raise ValueError("CSV header must be nickname,profile")
        for line, row in enumerate(reader, start=2):
            if None in row:
                raise ValueError(f"Line {line}: too many columns")
            name = (row[reader.fieldnames[0]] or "").strip()
            profile = (row[reader.fieldnames[1]] or "").strip()
            if not name or len(name) > 100:
                raise ValueError(f"Line {line}: nickname must have 1-100 characters")
            if profile not in PROFILE_IDS:
                raise ValueError(f"Line {line}: unknown profile {profile!r}")
            key = name.casefold()
            if key in rows:
                raise ValueError(f"Line {line}: duplicate nickname {name!r}")
            rows[key] = (name, PROFILE_IDS[profile])
    if not rows:
        raise ValueError("CSV has no data rows")
    return rows


def connection_string(raw: str) -> str:
    """Translate ADO.NET connection-string keys to ODBC Driver 17 keys."""
    names = {"server": "SERVER", "initial catalog": "DATABASE", "user id": "UID",
             "password": "PWD", "encrypt": "Encrypt", "trustservercertificate": "TrustServerCertificate",
             "connection timeout": "Connection Timeout"}
    parts = []
    for part in raw.split(";"):
        if not part:
            continue
        key, value = part.split("=", 1)
        if key.lower() not in names:
            continue
        if key.lower() in ("encrypt", "trustservercertificate"):
            value = {"true": "yes", "false": "no"}.get(value.lower(), value)
        parts.append(f"{names[key.lower()]}={value}")
    if not all(any(p.startswith(key + "=") for p in parts) for key in ("SERVER", "DATABASE", "UID", "PWD")):
        raise ValueError("SqlConnectionString must include server, database, user, and password")
    return "DRIVER={ODBC Driver 17 for SQL Server};" + ";".join(parts) + ";"


def plan(rows: dict[str, tuple[str, int]], existing: list[tuple[str, int]]) -> tuple[list, list, int]:
    current = {name.casefold(): (name, strategy) for name, strategy in existing}
    inserts, updates, unchanged = [], [], 0
    for key, (name, strategy) in rows.items():
        if key not in current:
            inserts.append((name, strategy))
        elif current[key][1] != strategy:
            updates.append((current[key][0], strategy))
        else:
            unchanged += 1
    return inserts, updates, unchanged


def import_csv(path: Path, *, casino: str, customer: str, apply: bool) -> tuple[int, int, int]:
    import pyodbc

    rows = read_profiles(path)  # Validate every row before opening a transaction.
    raw = os.environ.get("SqlConnectionString", "")
    if not raw:
        raise RuntimeError("SqlConnectionString environment variable is required")
    with pyodbc.connect(connection_string(raw), autocommit=False, timeout=15) as db:
        cursor = db.cursor()
        cursor.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
        customer_row = cursor.execute(
            "SELECT CustomerId FROM dbo.Customer WHERE CustomerName=? AND IsActive=1", customer).fetchone()
        if customer_row is None:
            raise ValueError(f"Active customer {customer!r} not found")
        linked = cursor.execute(
            "SELECT COUNT(*) FROM dbo.CustomerMachine WHERE CustomerId=? AND Casino=? AND IsActive=1",
            customer_row[0], casino).fetchone()[0]
        if not linked:
            raise ValueError(f"Customer {customer!r} has no active {casino!r} machine")
        existing = [(r.Username, r.Strategy) for r in cursor.execute(
            "SELECT Username, Strategy FROM dbo.BotAccount WITH (UPDLOCK, HOLDLOCK) WHERE Casino=?", casino)]
        inserts, updates, unchanged = plan(rows, existing)
        print(f"{casino} / {customer}: {len(rows)} CSV rows; insert {len(inserts)}, "
              f"update profile {len(updates)}, unchanged {unchanged}")
        if not apply:
            db.rollback()
            print("Dry run; database unchanged")
            return len(inserts), len(updates), unchanged
        for name, strategy in updates:
            cursor.execute(
                "UPDATE dbo.BotAccount SET Strategy=?, UpdatedUtc=SYSUTCDATETIME() "
                "WHERE Casino=? AND Username=?", strategy, casino, name)
            if cursor.rowcount != 1:
                raise RuntimeError(f"Expected one row when updating {name!r}")
        for name, strategy in inserts:
            cursor.execute("INSERT INTO dbo.BotAccount (Casino, Username, Strategy) VALUES (?,?,?)",
                           casino, name, strategy)
            if cursor.rowcount != 1:
                raise RuntimeError(f"Expected one row when inserting {name!r}")
        actual = {r.Username.casefold(): r.Strategy for r in cursor.execute(
            "SELECT Username, Strategy FROM dbo.BotAccount WHERE Casino=?", casino)}
        mismatches = [name for key, (name, strategy) in rows.items() if actual.get(key) != strategy]
        if mismatches:
            raise RuntimeError(f"Verification failed for {len(mismatches)} nicknames; rolled back")
        db.commit()
        print(f"Committed and verified {len(rows)} profiles")
        return len(inserts), len(updates), unchanged


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path)
    parser.add_argument("--casino", default="BluffPoker")
    parser.add_argument("--customer", default="Simo")
    parser.add_argument("--apply", action="store_true", help="Commit the validated changes")
    args = parser.parse_args()
    import_csv(args.csv, casino=args.casino, customer=args.customer, apply=args.apply)


if __name__ == "__main__":
    main()
