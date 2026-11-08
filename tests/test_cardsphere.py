import contextlib
import csv
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path

import mtg_cardsphere
import mtg_inventory


class CardsphereTests(unittest.TestCase):
    def test_export_preserves_printings_finishes_and_copies_without_db_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "inventory.sqlite3"
            target = Path(directory) / "haves.csv"
            with contextlib.closing(mtg_inventory.connect(database)) as db:
                for finish in ("nonfoil", "foil", "etched"):
                    mtg_inventory.insert(db, mtg_inventory.normalize(dict(
                        name='Example, "Card"', set_code="tst", scryfall_id="printing-id",
                        quantity=2, finish=finish, condition="near_mint")))
                mtg_inventory.insert(db, mtg_inventory.normalize(dict(
                    name="Not owned", set_code="tst", quantity=0)))
                db.commit()
            before = database.read_bytes()
            with contextlib.redirect_stdout(io.StringIO()) as output:
                mtg_cardsphere.export_haves(database, target)
            self.assertIn("3 entries, 6 copies", output.getvalue())
            self.assertEqual(before, database.read_bytes())
            with target.open(newline="") as file:
                rows = list(csv.DictReader(file))
            self.assertEqual({row["Foil"] for row in rows}, {"nonfoil", "foil", "etched"})
            self.assertTrue(all(row["Scryfall ID"] == "printing-id" for row in rows))
            self.assertTrue(all(row["Name"] == 'Example, "Card"' for row in rows))
            with self.assertRaises(FileExistsError):
                mtg_cardsphere.export_haves(database, target)

    def test_missing_database_and_invalid_entries_leave_no_export(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "inventory.sqlite3"
            target = Path(directory) / "haves.csv"
            with self.assertRaises(sqlite3.OperationalError):
                mtg_cardsphere.export_haves(database, target)
            self.assertFalse(database.exists())
            for changes in ({"condition": "unknown"}, {"scryfall_id": ""}, {"altered": 1}):
                with contextlib.closing(mtg_inventory.connect(database)) as db:
                    db.execute("DELETE FROM inventory")
                    values = dict(name="Example", set_code="tst", scryfall_id="printing-id",
                                  condition="near_mint")
                    values.update(changes)
                    mtg_inventory.insert(db, mtg_inventory.normalize(values))
                    db.commit()
                with self.assertRaises(ValueError):
                    mtg_cardsphere.export_haves(database, target)
                self.assertFalse(target.exists())
