"""Local MTG inventory. Python standard library only."""

import argparse
import csv
import hashlib
import io
import json
import sqlite3
import sys
from contextlib import closing
from decimal import Decimal, InvalidOperation
from pathlib import Path


FIELDS = (
    "name", "set_code", "set_name", "collector_number", "scryfall_id",
    "quantity", "finish", "condition", "language", "location",
    "purchase_price", "purchase_currency", "misprint", "altered", "notes",
)
DEFAULTS = dict.fromkeys(FIELDS, "")
DEFAULTS.update(quantity=1, finish="nonfoil", condition="unknown", language="en",
                misprint=0, altered=0)
ALIASES = {
    "card_name": "name", "card_number": "collector_number", "foil": "finish",
    "binder_name": "location", "binder": "location", "list_name": "location",
    "purchase_price_currency": "purchase_currency", "currency": "purchase_currency",
}


def connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version not in (0, 1):
        db.close()
        raise ValueError("Database schema is newer than this program supports")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS imports (
            id INTEGER PRIMARY KEY,
            sha256 TEXT NOT NULL UNIQUE,
            filename TEXT NOT NULL,
            row_count INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );
        CREATE TABLE IF NOT EXISTS inventory (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            set_code TEXT NOT NULL,
            set_name TEXT NOT NULL,
            collector_number TEXT NOT NULL,
            scryfall_id TEXT NOT NULL,
            quantity INTEGER NOT NULL CHECK (quantity >= 0),
            finish TEXT NOT NULL CHECK (finish IN ('nonfoil','foil','etched')),
            condition TEXT NOT NULL,
            language TEXT NOT NULL,
            location TEXT NOT NULL,
            purchase_price TEXT NOT NULL,
            purchase_currency TEXT NOT NULL,
            misprint INTEGER NOT NULL CHECK (misprint IN (0,1)),
            altered INTEGER NOT NULL CHECK (altered IN (0,1)),
            notes TEXT NOT NULL,
            import_id INTEGER REFERENCES imports(id),
            source_row INTEGER,
            raw_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );
        CREATE INDEX IF NOT EXISTS inventory_name ON inventory(name COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS inventory_scryfall ON inventory(scryfall_id);
        PRAGMA user_version = 1;
    """)
    return db


def boolean(value):
    value = str(value).strip().lower()
    if value in ("true", "yes", "1"):
        return 1
    if value in ("false", "no", "0", ""):
        return 0
    raise ValueError("expected true/false, yes/no, or 1/0")


def normalize(values):
    result = dict(DEFAULTS)
    result.update({key: str(value).strip() for key, value in values.items() if key in FIELDS})
    try:
        result["quantity"] = int(result["quantity"])
    except (ValueError, TypeError):
        raise ValueError("quantity must be a whole number")
    if result["quantity"] < 0:
        raise ValueError("quantity cannot be negative")
    if not result["scryfall_id"] and not (
        result["name"] and (result["set_code"] or result["set_name"])
    ):
        raise ValueError("provide a Scryfall ID, or a name plus set code/set name")
    result["set_code"] = result["set_code"].lower()
    result["finish"] = str(result["finish"]).lower()
    result["finish"] = {
        "normal": "nonfoil", "non-foil": "nonfoil", "false": "nonfoil",
        "no": "nonfoil", "0": "nonfoil", "true": "foil", "yes": "foil", "1": "foil",
    }.get(result["finish"], result["finish"])
    if result["finish"] not in ("nonfoil", "foil", "etched"):
        raise ValueError("finish must be nonfoil, foil, or etched")
    result["language"] = str(result["language"]).lower()
    result["condition"] = str(result["condition"]).lower()
    for field in ("misprint", "altered"):
        result[field] = boolean(result[field])
    if result["purchase_price"]:
        try:
            price = Decimal(result["purchase_price"])
        except InvalidOperation:
            raise ValueError("purchase price must be a decimal number")
        if not price.is_finite() or price < 0:
            raise ValueError("purchase price must be finite and nonnegative")
        result["purchase_price"] = format(price, "f")
        if not result["purchase_currency"]:
            raise ValueError("purchase currency is required with a purchase price")
    result["purchase_currency"] = result["purchase_currency"].upper()
    return result


def insert(db, values, import_id=None, source_row=None, raw=None):
    columns = list(FIELDS) + ["import_id", "source_row", "raw_json"]
    params = [values[field] for field in FIELDS] + [
        import_id, source_row, json.dumps(raw or {}, ensure_ascii=False),
    ]
    return db.execute(
        "INSERT INTO inventory ({}) VALUES ({})".format(
            ",".join(columns), ",".join("?" for _ in columns)
        ), params,
    ).lastrowid


def header_key(value):
    return "_".join(value.strip().lower().replace("_", " ").split())


def read_csv(data):
    text = data.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
    if not reader.fieldnames:
        raise ValueError("CSV needs a header row")
    mapped = [ALIASES.get(header_key(key), header_key(key)) for key in reader.fieldnames]
    known = [key for key in mapped if key in FIELDS]
    if len(known) != len(set(known)):
        raise ValueError("CSV has multiple columns mapping to the same inventory field")
    if not ("scryfall_id" in known or ("name" in known and (
        "set_code" in known or "set_name" in known
    ))):
        raise ValueError("CSV needs Scryfall ID, or Name plus Set code/Set name columns")
    rows = []
    errors = []
    for raw in reader:
        line = reader.line_num
        try:
            if None in raw or any(value is None for value in raw.values()):
                raise ValueError("row has a different number of columns than the header")
            values = {
                mapped[index]: raw[key] for index, key in enumerate(reader.fieldnames)
                if mapped[index] in FIELDS and raw[key].strip()
            }
            rows.append((line, normalize(values), raw))
        except ValueError as exc:
            errors.append("line {}: {}".format(line, exc))
    if errors:
        raise ValueError("CSV rejected; no cards added:\n" + "\n".join(errors))
    if not rows:
        raise ValueError("CSV contains no inventory rows")
    return rows


def import_csv(db, path, dry_run=False):
    data = Path(path).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    previous = db.execute("SELECT id FROM imports WHERE sha256 = ?", (digest,)).fetchone()
    if previous:
        raise ValueError("This exact file was already imported (batch {}).".format(previous[0]))
    rows = read_csv(data)
    copies = sum(values["quantity"] for _, values, _ in rows)
    if dry_run:
        return {"dry_run": True, "entries": len(rows), "copies": copies}
    with db:
        batch = db.execute(
            "INSERT INTO imports (sha256, filename, row_count) VALUES (?, ?, ?)",
            (digest, Path(path).name, len(rows)),
        ).lastrowid
        for line, values, raw in rows:
            insert(db, values, batch, line, raw)
    return {"import_id": batch, "entries": len(rows), "copies": copies}


def query(db, args):
    clauses, params = [], []
    for field in ("name", "set_code", "location", "finish", "scryfall_id"):
        value = getattr(args, field, None)
        if value is None:
            continue
        if field == "name":
            clauses.append("instr(lower(name), lower(?)) > 0")
        else:
            clauses.append(field + " = ? COLLATE NOCASE")
        params.append(value)
    if not getattr(args, "include_zero", False):
        clauses.append("quantity > 0")
    sql = "SELECT * FROM inventory"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    return [dict(row) for row in db.execute(sql + " ORDER BY name COLLATE NOCASE, id", params)]


def add_fields(parser, editing=False):
    for field in FIELDS:
        help_text = None
        if field == "purchase_price":
            help_text = "price per copy, as a decimal (requires purchase currency)"
        parser.add_argument("--" + field.replace("_", "-"), help=help_text)
    if editing:
        parser.add_argument("id", type=int, help="inventory entry ID")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/inventory.sqlite3", help="SQLite file path")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="create the inventory database")
    add_fields(commands.add_parser("add", help="add a separate inventory entry"))
    add_fields(commands.add_parser("edit", help="edit an entry; quantity is the new total"), True)
    importer = commands.add_parser("import-csv", help="add all rows from a CSV atomically")
    importer.add_argument("path")
    importer.add_argument("--dry-run", action="store_true", help="validate without adding inventory")
    for command in ("list", "export-csv"):
        sub = commands.add_parser(command)
        for field in ("name", "set_code", "location", "scryfall_id"):
            sub.add_argument("--" + field.replace("_", "-"))
        sub.add_argument("--finish", choices=("nonfoil", "foil", "etched"))
        sub.add_argument("--include-zero", action="store_true")
        if command == "export-csv":
            sub.add_argument("path")
    commands.add_parser("summary", help="count active entries and owned copies")
    args = parser.parse_args(argv)
    try:
        # Validate inputs before creating a database for common entry errors.
        values = {field: getattr(args, field) for field in FIELDS
                  if getattr(args, field, None) is not None} if args.command in ("add", "edit") else {}
        if args.command == "add":
            values = normalize(values)
        with closing(connect(args.db)) as db:
            if args.command == "init":
                print("Inventory ready: " + str(Path(args.db)))
            elif args.command == "add":
                with db:
                    entry_id = insert(db, values)
                print("Added entry {} ({} copies).".format(entry_id, values["quantity"]))
            elif args.command == "edit":
                existing = db.execute("SELECT * FROM inventory WHERE id = ?", (args.id,)).fetchone()
                if not existing:
                    raise ValueError("No inventory entry with ID {}".format(args.id))
                if not values:
                    raise ValueError("provide at least one field to edit")
                merged = {field: existing[field] for field in FIELDS}
                merged.update(values)
                merged = normalize(merged)
                with db:
                    db.execute("UPDATE inventory SET " + ", ".join(field + " = ?" for field in FIELDS)
                               + ", updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
                               [merged[field] for field in FIELDS] + [args.id])
                print("Updated entry {}.".format(args.id))
            elif args.command == "import-csv":
                print(json.dumps(import_csv(db, args.path, args.dry_run), indent=2))
            elif args.command in ("list", "export-csv"):
                rows = query(db, args)
                if args.command == "list":
                    print(json.dumps(rows, indent=2, ensure_ascii=False))
                else:
                    target = Path(args.path)
                    if target.resolve() == Path(args.db).resolve():
                        raise ValueError("export destination cannot be the database")
                    # Exclusive creation protects existing exports and source CSVs.
                    with target.open("x", encoding="utf-8", newline="") as output:
                        writer = csv.DictWriter(output, fieldnames=FIELDS, extrasaction="ignore")
                        writer.writeheader()
                        writer.writerows(rows)
                    print("Exported {} entries to {}.".format(len(rows), target))
            elif args.command == "summary":
                row = db.execute("SELECT count(*) AS entries, coalesce(sum(quantity),0) AS copies "
                                 "FROM inventory WHERE quantity > 0").fetchone()
                print(json.dumps(dict(row), indent=2))
        return 0
    except (ValueError, OSError, sqlite3.Error, csv.Error) as exc:
        print("Error: {}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
