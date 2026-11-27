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
from difflib import SequenceMatcher
from pathlib import Path


FIELDS = (
    "name", "set_code", "set_name", "collector_number", "scryfall_id",
    "quantity", "finish", "condition", "language", "location", "box",
    "purchase_price", "purchase_currency", "misprint", "altered", "notes",
)
SOURCE_FIELDS = ("source_filename", "source_row", "imported_at", "source_sha256", "source_archive")
PRINTING_FIELDS = ("set_code", "set_name", "collector_number")
VARIANT_FIELDS = ("finish", "language", "condition", "misprint", "altered")
SQLITE_NOCASE = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
SQLITE_MAX_INTEGER = 2**63 - 1
DEFAULTS = dict.fromkeys(FIELDS, "")
DEFAULTS.update(quantity=1, finish="nonfoil", condition="unknown", language="en",
                misprint=0, altered=0)
ALIASES = {
    "card_name": "name", "card_number": "collector_number", "foil": "finish",
    "binder_name": "location", "binder": "location", "list_name": "location",
    "box_name": "box",
    "purchase_price_currency": "purchase_currency", "currency": "purchase_currency",
}


def connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version not in (0, 1, 2, 3):
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
            box TEXT NOT NULL DEFAULT '',
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
    if "box" not in {row[1] for row in db.execute("PRAGMA table_info(inventory)")}:
        db.execute("ALTER TABLE inventory ADD COLUMN box TEXT NOT NULL DEFAULT ''")
    db.execute("PRAGMA user_version = 3")
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


def normalize(values, *, allow_name_only=False):
    result = dict(DEFAULTS)
    result.update({key: str(value).strip() for key, value in values.items() if key in FIELDS})
    try:
        result["quantity"] = int(result["quantity"])
    except (ValueError, TypeError):
        raise ValueError("quantity must be a whole number")
    if result["quantity"] < 0:
        raise ValueError("quantity cannot be negative")
    if not result["scryfall_id"] and not (
        result["name"] and (allow_name_only or result["set_code"] or result["set_name"])
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


def read_csv(data, *, box_operation=False):
    text = data.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
    if not reader.fieldnames:
        raise ValueError("CSV needs a header row")
    mapped = [ALIASES.get(header_key(key), header_key(key)) for key in reader.fieldnames]
    known = [key for key in mapped if key in FIELDS]
    if len(known) != len(set(known)):
        raise ValueError("CSV has multiple columns mapping to the same inventory field")
    if not ("scryfall_id" in known or ("name" in known and (
        box_operation or "set_code" in known or "set_name" in known
    ))):
        if box_operation:
            raise ValueError("CSV needs a Scryfall ID or Name column")
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
            if box_operation:
                if "quantity" not in values:
                    raise ValueError("provide a quantity")
                normalized = normalize(values, allow_name_only=True)
                if normalized["quantity"] > SQLITE_MAX_INTEGER:
                    raise ValueError("quantity is too large for SQLite")
                rows.append((line, {key: normalized[key] for key in values}, raw))
            else:
                rows.append((line, normalize(values), raw))
        except ValueError as exc:
            errors.append("line {}: {}".format(line, exc))
    if errors:
        message = "CSV rejected; no changes made" if box_operation else "CSV rejected; no cards added"
        raise ValueError(message + ":\n" + "\n".join(errors))
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


def match_key(value):
    return " ".join(str(value).casefold().split())


def match_score(search, value):
    if not search or not value:
        return 0
    if search == value:
        return 1
    if search in value or value in search:
        return 0.95
    return SequenceMatcher(None, search, value).ratio()


def review_input(prompt):
    try:
        return input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        raise ValueError("CSV review cancelled; no changes made") from None


def confirm(prompt):
    return review_input(prompt + " [y/N] ") in ("y", "yes")


def choose(title, choices):
    print(title)
    for index, label in enumerate(choices, 1):
        print(f"  {index}. {label}")
    indices = {str(index): index - 1 for index in range(1, len(choices) + 1)}
    while True:
        answer = review_input("Choose a number or press Enter to skip: ")
        if not answer:
            return None
        if answer in indices:
            return indices[answer]
        print(f"Enter a number from 1 to {len(choices)}.")


def resolve_box(boxes, label, role, *, allow_new=False):
    label = label.strip()
    # SQLite's NOCASE collation folds ASCII letters.
    key = label.translate(SQLITE_NOCASE)
    exact = next((box for box in boxes if box.translate(SQLITE_NOCASE) == key), None)
    if exact is not None:
        return exact
    scores = [(match_score(match_key(label), match_key(box)), box) for box in boxes]
    candidates = [box for score, box in sorted(scores, reverse=True) if score >= 0.72]
    if candidates:
        choices = [repr(box) for box in candidates]
        if allow_new:
            choices.append(f"Use new box {label!r}")
        selected = choose(f"Review {role} box {label!r}:", choices)
        if selected is not None:
            return (candidates + [label])[selected]
        raise ValueError("CSV review cancelled; no changes made")
    if allow_new and confirm(f"Use new destination box {label!r}?"):
        return label
    raise ValueError(f"No {role} box selected for {label!r}; no changes made")


def card_match(values, entry):
    card_id = match_key(values.get("scryfall_id", ""))
    entry_id = match_key(entry["scryfall_id"])
    if card_id and entry_id and card_id != entry_id:
        return 0
    exact_id = bool(card_id and card_id == entry_id)
    if any(field in values and match_key(values[field]) != match_key(entry[field])
           for field in VARIANT_FIELDS):
        return 0
    if exact_id:
        return 1
    # A known different printing is never a candidate for a partial name match.
    printing_fields = PRINTING_FIELDS
    if values.get("set_code") and entry["set_code"]:
        printing_fields = ("set_code", "collector_number")
    for field in printing_fields:
        actual = match_key(entry[field])
        if field in values and actual and match_key(values[field]) != actual:
            return 0
    score = match_score(match_key(values.get("name", "")), match_key(entry["name"]))
    exact_set = any(field in values and match_key(values[field]) == match_key(entry[field])
                    for field in ("set_code", "set_name"))
    missing_printing = any(field in values and not entry[field] for field in printing_fields)
    if score == 1 and (card_id or not exact_set or missing_printing):
        return 0.95
    return score


def describe_card(entry):
    label = entry.get("name") or entry["scryfall_id"]
    details = [entry.get(field) for field in (
        "set_code", "set_name", "collector_number", "finish", "condition", "language", "scryfall_id",
    )]
    details.extend(label for field, label in (("misprint", "misprint"), ("altered", "altered"))
                   if entry.get(field))
    return label + (" [" + ", ".join(value for value in details if value) + "]" if any(details) else "")


def describe_choice(entry, available):
    description = f"Entry {entry['id']}: {describe_card(entry)} ({available} copies)"
    for field in ("location", "notes"):
        if entry[field]:
            description += f", {field}={entry[field]!r}"
    if entry["purchase_price"]:
        description += f", paid {entry['purchase_price']} {entry['purchase_currency']} per copy"
    return description


def select_card(line, values, entries, remaining, *, adding, loose=False):
    matches = [(card_match(values, entry), entry) for entry in entries
               if adding or entry["quantity"] > 0]
    candidates = sorted(((score, entry) for score, entry in matches if score >= 0.72),
                        key=lambda match: (-match[0], match[1]["id"]))
    exact = [entry for score, entry in candidates if score == 1]
    if not adding:
        candidates = [(score, entry) for score, entry in candidates if remaining[entry["id"]] > 0]
        available = [entry for entry in exact if remaining[entry["id"]] > 0]
        if exact and not available:
            raise ValueError(f"Line {line}: no copies of {describe_card(values)} remain to move")
        exact = available
    if len(exact) == 1:
        return exact[0], False
    if exact:
        candidates = [(1, entry) for entry in exact]
    elif loose:
        name = match_key(values.get("name", ""))
        printing_id = match_key(values.get("scryfall_id", ""))
        candidates.extend((0, entry) for score, entry in matches
                          if score < 0.72 and (adding or remaining[entry["id"]] > 0)
                          and ((name and name == match_key(entry["name"])) or (
                              printing_id and printing_id == match_key(entry["scryfall_id"])
                          )))
    can_add = adding and not exact and (
        values.get("scryfall_id") or values.get("set_code") or values.get("set_name")
    )
    label = f"Line {line}: {describe_card(values)}, quantity {values['quantity']}"
    if not candidates:
        if can_add:
            return None, True
        print(label + " — no matching entry. Skipped.")
        return None, False
    choices = [
        describe_choice(entry, remaining[entry["id"]]) + (" [different card details]" if score == 0 else "")
        for score, entry in candidates
    ]
    if can_add:
        choices.append("Add as a new entry")
    selected = choose(label, choices)
    if selected is None:
        return None, False
    if selected == len(candidates):
        return None, True
    return candidates[selected][1], False


def box_entries(db, box):
    return [dict(row) for row in db.execute(
        "SELECT * FROM inventory WHERE box = ? COLLATE NOCASE ORDER BY id", (box,)
    )]


def card_identity(entry):
    printing = (match_key(entry["scryfall_id"]),) if entry["scryfall_id"] else tuple(
        match_key(entry[field]) for field in ("name", *PRINTING_FIELDS)
    )
    return printing + tuple(match_key(entry[field]) for field in VARIANT_FIELDS)


def plan_move(line, entry, quantity, entries, remaining):
    lots = [entry]
    if quantity > remaining[entry["id"]]:
        identity = card_identity(entry)
        lots.extend(other for other in entries if other["id"] != entry["id"]
                    and remaining[other["id"]] > 0 and card_identity(other) == identity)
    available = sum(remaining[lot["id"]] for lot in lots)
    if quantity > available:
        raise ValueError(f"Line {line}: {describe_card(entry)} has {available} copies left, "
                         f"but the CSV requests {quantity}")
    changes = []
    for lot in lots:
        before = remaining[lot["id"]]
        moved = min(quantity, before)
        changes.append((line, lot, before, moved))
        remaining[lot["id"]] -= moved
        quantity -= moved
        if not quantity:
            break
    return changes


def plan_box_changes(rows, entries, destination, *, moving, loose=False):
    remaining = {entry["id"]: entry["quantity"] for entry in entries}
    changes = []
    for line, values, _ in rows:
        quantity = values["quantity"]
        if not quantity:
            continue
        entry, add = select_card(line, values, entries, remaining, adding=not moving, loose=loose)
        if add:
            new_entry = normalize({**values, "box": destination})
            changes.append((line, new_entry, None, quantity))
        elif entry is not None:
            if moving:
                changes.extend(plan_move(line, entry, quantity, entries, remaining))
                continue
            entry_id = entry["id"]
            before = remaining[entry_id]
            total = before + quantity
            if total > SQLITE_MAX_INTEGER:
                raise ValueError(f"Line {line}: the new quantity is too large for SQLite")
            remaining[entry_id] = total
            changes.append((line, entry, before, quantity))
    return changes


def write_box_changes(db, changes, destination, *, moving):
    timestamp = "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')"
    for _, entry, before, quantity in changes:
        if before is None:
            insert(db, entry)
        elif moving and before == quantity:
            db.execute(f"UPDATE inventory SET box = ?, {timestamp} WHERE id = ?",
                       (destination, entry["id"]))
        elif moving:
            db.execute(f"UPDATE inventory SET quantity = ?, {timestamp} WHERE id = ?",
                       (before - quantity, entry["id"]))
            columns = (*FIELDS, "import_id", "source_row", "raw_json", "created_at")
            new_entry = {**entry, "box": destination, "quantity": quantity}
            db.execute(
                f"INSERT INTO inventory ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                [new_entry[column] for column in columns],
            )
        else:
            db.execute(f"UPDATE inventory SET quantity = ?, {timestamp} WHERE id = ?",
                       (before + quantity, entry["id"]))


def review_box_csv(db, args):
    moving = args.command == "move-csv"
    target = args.to_box if moving else args.box
    if not target.strip():
        raise ValueError("Provide a nonempty box label")
    rows = read_csv(Path(args.path).read_bytes(), box_operation=True)
    if moving and any(values["quantity"] == 0 for _, values, _ in rows):
        raise ValueError("Move quantities must be positive")
    boxes = [row[0] for row in db.execute(
        "SELECT DISTINCT box COLLATE NOCASE FROM inventory ORDER BY box COLLATE NOCASE"
    )]
    source = resolve_box(boxes, args.from_box, "source") if moving else None
    destination = resolve_box(boxes, target, "destination" if moving else "target", allow_new=moving)
    if moving and source == destination:
        raise ValueError("Choose different source and destination boxes")
    print(f"Move from {source!r} to {destination!r}" if moving else f"Add copies to box {destination!r}")
    affected = (source, destination) if moving else (destination,)
    snapshots = {box: box_entries(db, box) for box in affected}
    if not snapshots[affected[0]]:
        raise ValueError(f"Box {affected[0]!r} no longer has inventory entries")
    changes = plan_box_changes(rows, snapshots[affected[0]], destination, moving=moving, loose=args.loose)
    for line, entry, before, quantity in changes:
        label = describe_card(entry)
        if before is None:
            print(f"  Line {line}: add {quantity} copies of {label} (new entry)")
        elif moving:
            print(f"  Line {line}: move {quantity} of {before} copies of {label} (entry {entry['id']})")
        else:
            print(f"  Line {line}: add {quantity} copies of {label} (entry {entry['id']}), "
                  f"{before} to {before + quantity} copies")
    if not changes:
        print("No changes to apply.")
    elif args.dry_run:
        print("Dry run. No changes made.")
    elif confirm(f"Apply {len(changes)} changes?"):
        with db:
            db.execute("BEGIN IMMEDIATE")
            if any(box_entries(db, box) != entries for box, entries in snapshots.items()):
                raise ValueError("Inventory changed during review. Rerun the command to review the latest entries")
            write_box_changes(db, changes, destination, moving=moving)
        print(f"Applied {len(changes)} changes.")
    else:
        print("Cancelled. No changes made.")


def query(db, args):
    clauses, params = [], []
    for field in ("name", "set_code", "location", "box", "finish", "scryfall_id"):
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
    assigner = commands.add_parser("assign-box", help="assign a box to all entries from an import filename")
    assigner.add_argument("box", help="box label; use an empty string to clear")
    assigner.add_argument("--source-filename", required=True, help="original CSV filename, including extension")
    importer = commands.add_parser("import-csv", help="import CSV files or folders; archive originals")
    importer.add_argument("paths", nargs="*", help="files/folders; defaults to the database's inbox folder")
    importer.add_argument("--dry-run", action="store_true", help="validate without adding inventory")
    mover = commands.add_parser("move-csv", help="review and move copies between boxes using a CSV")
    mover.add_argument("path", help="CSV with card names or Scryfall IDs and quantities to move")
    mover.add_argument("--from-box", required=True, help="source box label")
    mover.add_argument("--to-box", required=True, help="destination box label")
    updater = commands.add_parser("update-box-csv", help="review and add copies to an existing box using a CSV")
    updater.add_argument("path", help="CSV with card names or Scryfall IDs and quantities to add")
    updater.add_argument("--box", required=True, help="existing box label")
    for command in (mover, updater):
        command.add_argument("--dry-run", action="store_true", help="review changes without writing them")
        command.add_argument("--loose", action="store_true",
                             help="offer matching names or Scryfall IDs with different card details")
    for command in ("list", "export-csv"):
        sub = commands.add_parser(command)
        for field in ("name", "set_code", "location", "box", "scryfall_id"):
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
        if args.command in ("move-csv", "update-box-csv") and not Path(args.db).is_file():
            raise ValueError("Inventory database does not exist")
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
            elif args.command == "assign-box":
                with db:
                    updated = db.execute(
                        "UPDATE inventory SET box = ?, "
                        "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                        "WHERE import_id IN (SELECT id FROM imports WHERE filename = ?)",
                        (args.box.strip(), args.source_filename),
                    ).rowcount
                    if not updated:
                        raise ValueError("No inventory entries from import filename: " + args.source_filename)
                print("Assigned box {!r} to {} entries.".format(args.box.strip(), updated))
            elif args.command == "import-csv":
                paths = args.paths or [str(Path(args.db).parent / "inbox")]
                result = import_paths(db, paths, args.dry_run)
                print(json.dumps(result, indent=2))
                if any(row["status"] == "error" for row in result["files"]):
                    return 1
            elif args.command in ("move-csv", "update-box-csv"):
                review_box_csv(db, args)
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
