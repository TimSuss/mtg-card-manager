import argparse
import contextlib
import csv
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path

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

    def test_manabox_preview_import_provenance_and_duplicate_protection(self):
        path = Path(__file__).resolve().parents[1] / "examples" / "manabox.csv"
        preview = inventory.import_csv(self.db, path, dry_run=True)
        self.assertEqual(preview, {"dry_run": True, "entries": 2, "copies": 5})
        self.assertEqual(self.db.execute("SELECT count(*) FROM imports").fetchone()[0], 0)
        result = inventory.import_csv(self.db, path)
        self.assertEqual(result["copies"], 5)
        rows = inventory.query(self.db, argparse.Namespace())
        self.assertEqual(rows[0]["finish"], "nonfoil")
        self.assertEqual(rows[0]["location"], "Trade binder")
        self.assertIn('"Rarity": "common"', rows[0]["raw_json"])
        self.assertEqual(rows[1]["purchase_price"], "2.25")
        with self.assertRaisesRegex(ValueError, "already imported"):
            inventory.import_csv(self.db, path)
        self.assertEqual(self.db.execute("SELECT sum(quantity) FROM inventory").fetchone()[0], 5)

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
