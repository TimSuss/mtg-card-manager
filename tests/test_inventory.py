import argparse
import contextlib
import csv
import io
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mtg_inventory as inventory


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "inventory.sqlite3"
        self.db = inventory.connect(self.db_path)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.db.close)

    def csv_file(self, text):
        path = self.root / "input.csv"
        path.write_text(text, encoding="utf-8-sig")
        return path

    def run_cli(self, *args):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                code = inventory.main(["--db", str(self.db_path), *args])
        return code, output.getvalue(), errors.getvalue()

    def test_snapshot_restores_imports_manual_edits_and_archive_links(self):
        self.db.execute("PRAGMA journal_mode = WAL")
        source = self.csv_file("Name,Set code,Quantity\nSol Ring,cmm,2\n")
        inventory.import_csv(self.db, source)
        self.assertEqual(self.run_cli("edit", "1", "--quantity", "5", "--notes", "Keep this")[0], 0)
        self.assertEqual(self.run_cli("backup")[0], 0)
        snapshot = self.root / "snapshot.sqlite3"
        restored = self.root / "restored"
        restored.mkdir()
        shutil.copy2(snapshot, restored / "inventory.sqlite3")
        shutil.copytree(self.root / "imports", restored / "imports")
        with contextlib.closing(inventory.connect(restored / "inventory.sqlite3")) as db:
            row = inventory.query(db, argparse.Namespace())[0]
            self.assertEqual(row["quantity"], 5)
            self.assertEqual(row["notes"], "Keep this")
            self.assertEqual((restored / row["source_archive"]).read_bytes(), source.read_bytes())
            self.assertEqual(inventory.import_csv(db, source)["status"], "already_imported")

    def test_failed_refresh_preserves_previous_snapshot(self):
        inventory.backup_database(self.db)
        snapshot = self.root / "snapshot.sqlite3"
        previous = snapshot.read_bytes()
        with mock.patch.object(inventory.os, "replace", side_effect=OSError("test failure")):
            with self.assertRaises(OSError):
                inventory.backup_database(self.db)
        self.assertEqual(snapshot.read_bytes(), previous)
        self.assertEqual(list(self.root.glob(".snapshot-*")), [])

    def test_backup_refuses_missing_database_and_self_overwrite(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(inventory.main(["--db", str(self.root / "missing.sqlite3"), "backup"]), 1)
        self.assertFalse((self.root / "missing.sqlite3").exists())
        with contextlib.closing(inventory.connect(self.root / "snapshot.sqlite3")) as db:
            with self.assertRaisesRegex(ValueError, "itself"):
                inventory.backup_database(db)

    def test_manabox_preview_import_provenance_and_duplicate_protection(self):
        path = Path(__file__).resolve().parents[1] / "examples" / "manabox.csv"
        preview = inventory.import_csv(self.db, path, dry_run=True)
        self.assertEqual(preview, {"status": "ready", "dry_run": True, "entries": 2, "copies": 5})
        self.assertFalse((self.root / "imports").exists())
        self.assertEqual(self.db.execute("SELECT count(*) FROM imports").fetchone()[0], 0)
        result = inventory.import_csv(self.db, path)
        self.assertEqual(result["copies"], 5)
        rows = inventory.query(self.db, argparse.Namespace())
        self.assertEqual(rows[0]["finish"], "nonfoil")
        self.assertEqual(rows[0]["location"], "Trade binder")
        self.assertIn('"Rarity": "common"', rows[0]["raw_json"])
        self.assertEqual(rows[1]["purchase_price"], "2.25")
        for index, row in enumerate(rows):
            self.assertEqual(row["source_filename"], "manabox.csv")
            self.assertEqual(row["source_row"], index + 2)
            self.assertEqual(row["import_id"], result["import_id"])
            self.assertEqual(len(row["source_sha256"]), 64)
            self.assertTrue(row["imported_at"].endswith("Z"))
        self.assertEqual(inventory.import_csv(self.db, path)["status"], "already_imported")
        self.assertEqual(self.db.execute("SELECT sum(quantity) FROM inventory").fetchone()[0], 5)

    def test_archive_is_exact_and_survives_original_removal(self):
        path = self.csv_file("Name,Set code\r\nSol Ring,cmm\r\n")
        original = path.read_bytes()
        result = inventory.import_csv(self.db, path)
        archive = self.root / result["archive_path"]
        self.assertEqual(archive.read_bytes(), original)
        path.unlink()
        row = inventory.query(self.db, argparse.Namespace())[0]
        self.assertEqual(row["source_archive"], result["archive_path"])
        self.assertEqual(archive.read_bytes(), original)

    def test_archive_failure_rolls_back_inventory_and_import(self):
        path = self.csv_file("Name,Set code\nSol Ring,cmm\n")
        with mock.patch.object(inventory, "write_archive", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                inventory.import_csv(self.db, path)
        self.assertEqual(self.db.execute("SELECT count(*) FROM inventory").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM imports").fetchone()[0], 0)

    def test_existing_archive_is_verified_and_never_overwritten(self):
        path = self.csv_file("Name,Set code\nSol Ring,cmm\n")
        result = inventory.import_csv(self.db, path)
        archive = self.root / result["archive_path"]
        archive.write_bytes(b"changed archive")
        with self.assertRaisesRegex(ValueError, "unexpected contents"):
            inventory.import_csv(self.db, path)
        self.assertEqual(archive.read_bytes(), b"changed archive")
        self.assertEqual(self.db.execute("SELECT count(*) FROM inventory").fetchone()[0], 1)

    def test_folder_import_preview_duplicates_errors_and_repeat(self):
        inbox = self.root / "inbox"
        inbox.mkdir()
        valid = b"Name,Set code,Quantity\nSol Ring,cmm,2\n"
        (inbox / "a.csv").write_bytes(valid)
        (inbox / "b.CSV").write_bytes(b"Name,Set code,Quantity\nLightning Bolt,m10,4\n")
        (inbox / "c.csv").write_bytes(valid)
        (inbox / "invalid.csv").write_text("Name,Set code,Quantity\nBad,cmm,-1\n")
        (inbox / "ignored.txt").write_text("ignored")
        preview = inventory.import_paths(self.db, [inbox], dry_run=True)
        self.assertEqual(preview["copies"], 6)
        self.assertFalse((self.root / "imports").exists())
        self.assertEqual(self.db.execute("SELECT count(*) FROM inventory").fetchone()[0], 0)
        code, _, _ = self.run_cli("import-csv")
        self.assertEqual(code, 1)  # Valid files commit; invalid file is reported.
        self.assertEqual(self.db.execute("SELECT sum(quantity) FROM inventory").fetchone()[0], 6)
        self.assertEqual(len(list((self.root / "imports").glob("*.csv"))) +
                         len(list((self.root / "imports").glob("*.CSV"))), 2)
        again = inventory.import_paths(self.db, [inbox])
        self.assertEqual(again["copies"], 0)
        self.assertEqual([row["status"] for row in again["files"]],
                         ["already_imported", "already_imported", "duplicate_in_batch", "error"])

    def test_several_explicit_files(self):
        first = self.csv_file("Name,Set code\nSol Ring,cmm\n")
        second = self.root / "second.csv"
        second.write_text("Name,Set code\nLightning Bolt,m10\n")
        code, _, _ = self.run_cli("import-csv", str(first), str(second))
        self.assertEqual(code, 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM imports").fetchone()[0], 2)

    def test_old_database_migration_and_archive_backfill(self):
        legacy = self.root / "legacy.sqlite3"
        path = self.csv_file("Name,Set code\nSol Ring,cmm\n")
        import hashlib
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        with sqlite3.connect(str(legacy)) as old:
            old.executescript("""CREATE TABLE imports (
                id INTEGER PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE, filename TEXT NOT NULL,
                row_count INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT '2026-01-01');
                PRAGMA user_version = 1;""")
            old.execute("INSERT INTO imports (sha256, filename, row_count) VALUES (?, ?, ?)",
                        (digest, path.name, 1))
        with contextlib.closing(inventory.connect(legacy)) as upgraded:
            self.assertEqual(upgraded.execute("PRAGMA user_version").fetchone()[0], 3)
            result = inventory.import_csv(upgraded, path)
            self.assertEqual(result["status"], "already_imported")
            self.assertEqual((self.root / result["archive_path"]).read_bytes(), path.read_bytes())
            self.assertEqual(upgraded.execute("SELECT count(*) FROM imports").fetchone()[0], 1)

    def test_source_survives_edit_reopen_and_export(self):
        path = self.csv_file("Name,Set code\nSol Ring,cmm\n")
        inventory.import_csv(self.db, path)
        self.assertEqual(self.run_cli("edit", "1", "--quantity", "2")[0], 0)
        with contextlib.closing(inventory.connect(self.db_path)) as reopened:
            row = inventory.query(reopened, argparse.Namespace(name="Sol"))[0]
        self.assertEqual(row["source_filename"], "input.csv")
        self.assertEqual(row["source_row"], 2)
        self.assertEqual(row["quantity"], 2)
        export = self.root / "export.csv"
        self.assertEqual(self.run_cli("export-csv", str(export))[0], 0)
        with export.open(newline="", encoding="utf-8") as file:
            exported = next(csv.DictReader(file))
        for field in inventory.SOURCE_FIELDS:
            self.assertEqual(exported[field], str(row[field]))

    def test_existing_inventory_migrates_without_losing_data(self):
        legacy = self.root / "legacy-box.sqlite3"
        schema = self.db.execute("SELECT sql FROM sqlite_master WHERE name = 'inventory'").fetchone()[0]
        schema = schema.replace("box TEXT NOT NULL DEFAULT '',", "")
        fields = [field for field in inventory.FIELDS if field != "box"]
        values = inventory.normalize({"name": "Sol Ring", "set_code": "cmm", "quantity": "3",
                                      "location": "Commander binder", "notes": "Keep this"})
        with sqlite3.connect(str(legacy)) as old:
            old.execute(schema)
            old.execute("INSERT INTO inventory ({}) VALUES ({})".format(
                ",".join(fields), ",".join("?" for _ in fields)), [values[field] for field in fields])
            old.execute("PRAGMA user_version = 2")
        with contextlib.closing(inventory.connect(legacy)) as upgraded:
            row = inventory.query(upgraded, argparse.Namespace())[0]
            self.assertEqual(row["box"], "")
            self.assertEqual(row["id"], 1)
            for field in fields:
                self.assertEqual(row[field], values[field])

    def test_box_manual_edits_filters_and_csv_roundtrip(self):
        self.assertEqual(self.run_cli("add", "--name", "Sol Ring", "--set-code", "cmm",
                                      "--box", "Box 1")[0], 0)
        self.assertEqual(self.run_cli("edit", "1", "--box", "Box 2")[0], 0)
        self.assertEqual(len(inventory.query(self.db, argparse.Namespace(box="box 2"))), 1)
        self.assertEqual(inventory.query(self.db, argparse.Namespace(box="Box 1")), [])
        target = self.root / "boxes.csv"
        self.assertEqual(self.run_cli("export-csv", str(target), "--box", "BOX 2")[0], 0)
        with contextlib.closing(inventory.connect(self.root / "roundtrip.sqlite3")) as db:
            inventory.import_csv(db, target)
            self.assertEqual(inventory.query(db, argparse.Namespace())[0]["box"], "Box 2")
        self.assertEqual(self.run_cli("edit", "1", "--box", "")[0], 0)
        self.assertEqual(len(inventory.query(self.db, argparse.Namespace(box=""))), 1)

    def test_box_csv_header_and_bulk_assignment_preserve_provenance(self):
        path = self.csv_file("Name,Set code,Box name,Binder name\nSol Ring,cmm,Box 1,Commander binder\n")
        inventory.import_csv(self.db, path)
        other = self.root / "other.csv"
        other.write_text("Name,Set code,Box\nLightning Bolt,m10,Box 3\n")
        inventory.import_csv(self.db, other)
        before = inventory.query(self.db, argparse.Namespace(name="Sol"))[0]
        self.assertEqual(before["box"], "Box 1")
        self.assertEqual(self.run_cli("assign-box", " Box 2 ", "--source-filename", "input.csv")[0], 0)
        after = inventory.query(self.db, argparse.Namespace(box="Box 2"))[0]
        self.assertEqual(after["location"], "Commander binder")
        for field in (*inventory.SOURCE_FIELDS, "raw_json", "import_id", "quantity"):
            self.assertEqual(after[field], before[field])
        self.assertEqual(len(inventory.query(self.db, argparse.Namespace(box="Box 3"))), 1)
        self.assertEqual(self.run_cli("assign-box", "Box 4", "--source-filename", "missing.csv")[0], 1)

    def test_bad_row_rejects_entire_file(self):
        path = self.csv_file("Name,Set code,Quantity\nSol Ring,cmm,1\nLightning Bolt,m10,-2\n")
        with self.assertRaisesRegex(ValueError, "line 3"):
            inventory.import_csv(self.db, path)
        self.assertEqual(self.db.execute("SELECT count(*) FROM inventory").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM imports").fetchone()[0], 0)

    def test_database_failure_rolls_back_batch_and_prior_rows(self):
        path = self.csv_file("Name,Set code\nSol Ring,cmm\nLightning Bolt,m10\n")
        self.db.execute("""CREATE TRIGGER fail_second BEFORE INSERT ON inventory
                        WHEN NEW.name = 'Lightning Bolt'
                        BEGIN SELECT RAISE(ABORT, 'test failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            inventory.import_csv(self.db, path)
        self.assertEqual(self.db.execute("SELECT count(*) FROM imports").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM inventory").fetchone()[0], 0)

    def test_quoted_names_collectors_and_unknown_prices(self):
        path = self.csv_file('Name,Set code,Collector number,Foil,Quantity\n"Example, Card",TST,001a,etched,2\n')
        inventory.import_csv(self.db, path)
        row = inventory.query(self.db, argparse.Namespace())[0]
        self.assertEqual(row["name"], "Example, Card")
        self.assertEqual(row["collector_number"], "001a")
        self.assertEqual(row["finish"], "etched")
        self.assertEqual(row["purchase_price"], "")

    def test_manual_edit_query_zero_quantity_and_export_roundtrip(self):
        self.assertEqual(self.run_cli("add", "--name", "Sol Ring", "--set-code", "CMM",
                                      "--quantity", "3", "--purchase-price", "0.10",
                                      "--purchase-currency", "usd")[0], 0)
        self.assertEqual(self.run_cli("edit", "1", "--location", "Trade binder")[0], 0)
        rows = inventory.query(self.db, argparse.Namespace(name="SOL", location="trade binder"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["purchase_price"], "0.10")
        for field in inventory.SOURCE_FIELDS:
            self.assertIsNone(rows[0][field])
        export = self.root / "export.csv"
        self.assertEqual(self.run_cli("export-csv", str(export))[0], 0)
        with contextlib.closing(inventory.connect(self.root / "other.sqlite3")) as other:
            inventory.import_csv(other, export)
            copy = inventory.query(other, argparse.Namespace())[0]
        for field in inventory.FIELDS:
            self.assertEqual(copy[field], rows[0][field])
        self.assertEqual(self.run_cli("export-csv", str(export))[0], 1)
        self.assertEqual(self.run_cli("edit", "1", "--quantity", "0")[0], 0)
        self.assertEqual(inventory.query(self.db, argparse.Namespace()), [])
        self.assertEqual(len(inventory.query(self.db, argparse.Namespace(include_zero=True))), 1)

    def test_invalid_values_do_not_add_inventory(self):
        for flags in (
            ("--quantity", "1.5"), ("--purchase-price", "NaN", "--purchase-currency", "USD"),
            ("--purchase-price", "2.00"), ("--finish", "rainbow"), ("--misprint", "maybe"),
        ):
            with self.subTest(flags=flags):
                self.assertEqual(self.run_cli("add", "--name", "Sol Ring", "--set-code", "cmm", *flags)[0], 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM inventory").fetchone()[0], 0)

    def test_malformed_and_ambiguous_csv_rejected(self):
        for text in (
            "Name,Set code,Quantity\nSol Ring,cmm,1,extra\n",
            "Name,Set code,Quantity\nSol Ring,cmm\n",
            "Name,Card name,Set code\nSol Ring,Sol Ring,cmm\n",
            "Name,Quantity\nSol Ring,1\n",
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                inventory.import_csv(self.db, self.csv_file(text))


if __name__ == "__main__":
    unittest.main()
