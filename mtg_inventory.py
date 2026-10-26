"""Local MTG inventory. Python standard library only."""

import argparse
import csv
import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from decimal import Decimal, InvalidOperation
from pathlib import Path


FIELDS = (
    "name", "set_code", "set_name", "collector_number", "scryfall_id",
    "quantity", "finish", "condition", "language", "location",
    "purchase_price", "purchase_currency", "misprint", "altered", "notes",
)
SOURCE_FIELDS = ("source_filename", "source_row", "imported_at", "source_sha256", "source_archive")
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
    if version not in (0, 1, 2):
        db.close()
        raise ValueError("Database schema is newer than this program supports")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS imports (
            id INTEGER PRIMARY KEY,
            sha256 TEXT NOT NULL UNIQUE,
            filename TEXT NOT NULL,
            row_count INTEGER NOT NULL,
            archive_path TEXT,
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
    """)
    if "archive_path" not in {row[1] for row in db.execute("PRAGMA table_info(imports)")}:
        db.execute("ALTER TABLE imports ADD COLUMN archive_path TEXT")
    db.execute("PRAGMA user_version = 2")
    db.commit()
    return db


def boolean(value):
    value = str(value).strip().lower()
    if value in ("true", "yes", "1"):
        return 1
    if value in ("false", "no", "0", ""):
        return 0
    raise ValueError("expected true/false, yes/no, or 1/0")


def backup_database(db):
    """Atomically refresh a standalone, consistent snapshot for Git."""
    filename = db.execute("PRAGMA database_list").fetchone()[2]
    if not filename:
        raise ValueError("Backup requires a file-backed database")
    source = Path(filename)
    target = source.with_name("snapshot.sqlite3")
    if target.resolve() == source.resolve():
        raise ValueError("Cannot back up a database named snapshot.sqlite3 to itself")
    descriptor, temporary = tempfile.mkstemp(prefix=".snapshot-", suffix=".sqlite3", dir=source.parent)
    os.close(descriptor)
    temporary = Path(temporary)
    try:
        with closing(sqlite3.connect(str(temporary))) as destination:
            db.backup(destination)
            check = destination.execute("PRAGMA quick_check").fetchall()
            if check != [("ok",)]:
                raise ValueError("Database snapshot failed its integrity check")
        os.replace(str(temporary), str(target))
    finally:
        temporary.unlink(missing_ok=True)
    return target


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


def archive_target(db, digest, filename):
    database = db.execute("PRAGMA database_list").fetchone()[2]
    if not database:
        raise ValueError("CSV archiving requires a file-backed database")
    # Relative paths keep archives portable when the database moves to the Pi.
    relative = Path("imports") / (digest + "_" + filename[:150])
    return relative.as_posix(), Path(database).parent / relative


def write_archive(target, data):
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        output = target.open("xb")
    except FileExistsError:
        if target.read_bytes() != data:
            raise ValueError("Archive exists with unexpected contents: {}".format(target))
        return False
    try:
        with output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        target.unlink()
        raise
    return True


def import_csv(db, path, dry_run=False, *, data=None):
    if data is None:
        data = Path(path).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    previous = db.execute("SELECT * FROM imports WHERE sha256 = ?", (digest,)).fetchone()
    if previous:
        relative, target = archive_target(db, digest, previous["filename"])
        if not dry_run:
            # Also backfill an archive for imports made before archiving was added.
            with db:
                write_archive(target, data)
                db.execute("UPDATE imports SET archive_path = ? WHERE id = ?",
                           (relative, previous["id"]))
        return {"status": "already_imported", "import_id": previous["id"],
                "entries": 0, "copies": 0, "archive_path": relative, "dry_run": dry_run}
    rows = read_csv(data)
    copies = sum(values["quantity"] for _, values, _ in rows)
    if dry_run:
        return {"status": "ready", "dry_run": True, "entries": len(rows), "copies": copies}
    relative, target = archive_target(db, digest, Path(path).name)
    created = False
    try:
        with db:
            batch = db.execute(
                "INSERT INTO imports (sha256, filename, row_count, archive_path) VALUES (?, ?, ?, ?)",
                (digest, Path(path).name, len(rows), relative),
            ).lastrowid
            for line, values, raw in rows:
                insert(db, values, batch, line, raw)
            # Archive before commit, so a successful import always has its original file.
            created = write_archive(target, data)
    except Exception:
        if created:
            target.unlink()
        raise
    return {"status": "imported", "import_id": batch, "entries": len(rows),
            "copies": copies, "archive_path": relative}


def import_paths(db, paths, dry_run=False):
    """Process files independently; keep going and report every failure."""
    files, results = [], []
    for value in paths:
        path = Path(value)
        if path.is_dir():
            children = sorted((p for p in path.iterdir() if p.is_file() and p.suffix.lower() == ".csv"),
                              key=lambda p: p.name)
            if not children:
                results.append({"file": str(path), "status": "empty_folder"})
            files.extend(children)
        else:
            files.append(path)
    seen = set()
    for path in files:
        try:
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            if digest in seen:
                results.append({"file": str(path), "status": "duplicate_in_batch",
                                "entries": 0, "copies": 0})
                continue
            result = import_csv(db, path, dry_run, data=data)
            seen.add(digest)
            results.append({"file": str(path), **result})
        except (ValueError, OSError, sqlite3.Error, csv.Error) as exc:
            results.append({"file": str(path), "status": "error", "error": str(exc)})
    return {"dry_run": dry_run, "entries": sum(r.get("entries", 0) for r in results),
            "copies": sum(r.get("copies", 0) for r in results), "files": results}


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
    sql = ("SELECT inventory.*, imports.filename AS source_filename, "
           "imports.created_at AS imported_at, imports.sha256 AS source_sha256, "
           "imports.archive_path AS source_archive "
           "FROM inventory LEFT JOIN imports ON inventory.import_id = imports.id")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    return [dict(row) for row in db.execute(
        sql + " ORDER BY name COLLATE NOCASE, inventory.id", params
    )]


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
    importer = commands.add_parser("import-csv", help="import CSV files or folders; archive originals")
    importer.add_argument("paths", nargs="*", help="files/folders; defaults to the database's inbox folder")
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
    commands.add_parser("backup", help="refresh snapshot.sqlite3 beside the database for Git")
    args = parser.parse_args(argv)
    try:
        if args.command == "backup" and not Path(args.db).is_file():
            raise ValueError("Database does not exist; refusing to replace a snapshot with an empty database")
        # Validate inputs before creating a database for common entry errors.
        values = {field: getattr(args, field) for field in FIELDS
                  if getattr(args, field, None) is not None} if args.command in ("add", "edit") else {}
        if args.command == "add":
            values = normalize(values)
        with closing(connect(args.db)) as db:
            if args.command == "init":
                (Path(args.db).parent / "inbox").mkdir(parents=True, exist_ok=True)
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
                paths = args.paths or [str(Path(args.db).parent / "inbox")]
                result = import_paths(db, paths, args.dry_run)
                print(json.dumps(result, indent=2))
                if any(row["status"] == "error" for row in result["files"]):
                    return 1
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
                        writer = csv.DictWriter(output, fieldnames=FIELDS + SOURCE_FIELDS,
                                                extrasaction="ignore")
                        writer.writeheader()
                        writer.writerows(rows)
                    print("Exported {} entries to {}.".format(len(rows), target))
            elif args.command == "summary":
                row = db.execute("SELECT count(*) AS entries, coalesce(sum(quantity),0) AS copies "
                                 "FROM inventory WHERE quantity > 0").fetchone()
                print(json.dumps(dict(row), indent=2))
            elif args.command == "backup":
                target = backup_database(db)
                print("Database snapshot ready: {}\nCommit this file and the imports/ folder beside it.".format(target))
        return 0
    except (ValueError, OSError, sqlite3.Error, csv.Error) as exc:
        print("Error: {}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
