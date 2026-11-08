"""Export all owned cards for Cardsphere's ManaBox-compatible CSV importer."""

import argparse
import csv
import sqlite3
import sys
from contextlib import closing
from pathlib import Path


HEADERS = ("Name", "Set code", "Set name", "Collector number", "Foil",
           "Quantity", "Scryfall ID", "Language", "Condition")
CONDITIONS = {
    "near_mint": "near_mint", "nm": "near_mint",
    "slightly_played": "slightly_played", "sp": "slightly_played",
    "moderately_played": "moderately_played", "mp": "moderately_played",
    "heavily_played": "heavily_played", "hp": "heavily_played",
    # Preserve ManaBox's EU grades for Cardsphere's own translation.
    "mint": "mint", "excellent": "excellent", "good": "good",
    "light_played": "light_played", "played": "played",
}


def export_haves(database, destination):
    database, destination = Path(database).resolve(), Path(destination).resolve()
    if database == destination:
        raise ValueError("Export destination cannot be the database")
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT * FROM inventory WHERE quantity > 0 ORDER BY name, id").fetchall()
    output = []
    for row in rows:
        label = "Inventory entry {} ({})".format(row["id"], row["name"])
        if not row["scryfall_id"].strip():
            raise ValueError(label + " needs a Scryfall ID to identify its printing")
        condition = row["condition"].strip().lower().replace(" ", "_").replace("-", "_")
        if condition not in CONDITIONS:
            raise ValueError(label + " has an unsupported/unknown condition: " + row["condition"])
        if row["finish"] not in ("nonfoil", "foil", "etched"):
            raise ValueError(label + " has an unsupported finish")
        if not row["language"].strip():
            raise ValueError(label + " needs a language")
        if row["misprint"] or row["altered"]:
            raise ValueError(label + " is altered or misprinted; review before listing on Cardsphere")
        output.append((row["name"], row["set_code"], row["set_name"],
                       row["collector_number"], row["finish"], row["quantity"],
                       row["scryfall_id"], row["language"], CONDITIONS[condition]))
    # Validate everything before creating a file; never overwrite an existing export.
    with destination.open("x", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(HEADERS)
        writer.writerows(output)
    copies = sum(row["quantity"] for row in rows)
    etched = sum(row["quantity"] for row in rows if row["finish"] == "etched")
    print("Exported {} entries, {} copies to {}".format(len(rows), copies, destination))
    print("Etched copies: {}. Verify finishes and matched printings in Cardsphere's preview.".format(etched))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/inventory.sqlite3")
    parser.add_argument("path", help="new CSV file to create")
    args = parser.parse_args(argv)
    try:
        export_haves(args.db, args.path)
    except (ValueError, OSError, sqlite3.Error, csv.Error) as exc:
        print("Error: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
