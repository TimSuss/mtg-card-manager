# MTG card manager

A local inventory stored in **SQLite**, with manual entry and CSV import/export.
Requires Python 3.9 or newer; no third-party packages or database server required.

## Why SQLite?

The inventory lives in one file, `data/inventory.sqlite3`. SQLite provides transactions,
validation, and SQL queries without running a server. It also lets us add price history
and metrics later without rewriting the collection as a giant JSON document.
CSV is the exchange format for spreadsheets and ManaBox, rather than the primary store.

See [SQLite's application file format documentation](https://www.sqlite.org/appfileformat.html)
and [ManaBox's import/export guide](https://manabox.app/guides/collection/import-export/).

## Start an inventory

Run these commands from this directory:

```sh
python3 mtg_inventory.py init
python3 mtg_inventory.py add --name "Lightning Bolt" --set-code m10 --collector-number 146 --quantity 4 --location "Trade binder"
python3 mtg_inventory.py list
python3 mtg_inventory.py summary
```

Use `--db /path/to/collection.sqlite3` **before** the command to select a different file.
Commands create the database if needed. Your actual inventory is ignored by Git.

Each entry is a lot: a quantity of copies sharing the same printing, finish, condition,
language, location, and per-copy purchase price. Adding the same card again creates a
separate entry; entries are never silently merged. This preserves different purchases
and binder locations. A card name alone does not identify its printing.

Record a Scryfall ID when available, or a name plus set code/set name. Include the
collector number to distinguish versions within a set; it is text, so leading zeros
and letter suffixes survive. Entries without a collector number or Scryfall ID are
allowed but may need enrichment before an exact price lookup. No online card lookup
or verification happens yet.

Defaults are quantity `1`, finish `nonfoil`, condition `unknown`, and language `en`.
Language and condition are lowercased free text; use consistent values (for example,
`en` and `near_mint`). The importer does not equate `English` with `en`.

```sh
python3 mtg_inventory.py add --name "Sol Ring" --set-code cmm --collector-number 410 --finish foil --condition near_mint --language en --purchase-price 2.25 --purchase-currency USD
python3 mtg_inventory.py edit 1 --quantity 3 --location "Commander box"
python3 mtg_inventory.py edit 1 --quantity 0
```

`edit` uses the ID shown by `list` and changes only specified fields. Quantity is the
**new total**, not an increment. Setting it to zero keeps the entry but hides it from
ordinary lists and exports; use `--include-zero` to include it. Prices are optional
decimal strings per copy, with currency required when supplied. An unknown price stays
blank rather than becoming zero. To clear a text field, pass an empty value, such as
`--notes ""` or `--purchase-price ""`.

## Import a CSV

```sh
python3 mtg_inventory.py import-csv /path/to/manabox.csv --dry-run
python3 mtg_inventory.py import-csv /path/to/manabox.csv
```

The preview reports entries and total copies. All rows must validate before any are
added; errors report CSV line numbers. The import is a single transaction. UTF-8 CSVs
with or without a byte order mark are supported, including quoted commas in names.
Files must use commas as separators and periods in decimal prices.

Supported headers are case insensitive; spaces and underscores are interchangeable:

| Inventory field | Accepted CSV headers |
| --- | --- |
| Name | Name, Card name |
| Set | Set code, Set name (separate fields) |
| Collector number | Collector number, Card number |
| Printing ID | Scryfall ID |
| Quantity | Quantity |
| Finish | Finish, Foil |
| Condition / language | Condition, Language |
| Location | Location, Binder name, Binder, List name |
| Purchase price | Purchase price |
| Currency | Purchase currency, Purchase price currency, Currency |
| Other copy details | Misprint, Altered, Notes |

Finish accepts `normal`, `nonfoil`, `foil`, `etched`, or boolean values for foil/nonfoil.
Misprint and altered accept true/false, yes/no, or 1/0. Empty cells use defaults.
`examples/manabox.csv` is a representative sample, **not your inventory**.
Test it with a disposable database if desired:

```sh
python3 mtg_inventory.py --db /tmp/mtg-demo.sqlite3 import-csv examples/manabox.csv
```

The database retains each original CSV row as JSON, its source line, import timestamp,
filename, and file hash. Unrecognized columns (including ManaBox IDs or price-related
columns) remain available in `raw_json`; they do not become current market prices.

**Imports add inventory; they do not synchronize a collection snapshot.** Importing
the exact same file again is rejected, even under a different filename. Changed files
are treated as new additions: a later full ManaBox export can therefore double-count
existing cards. For now, import your initial collection once, then import files containing
only new additions or use `edit` for corrections. Byte-level duplicate protection does
not detect equivalent files with reordered rows or changed whitespace. There is no
import undo or snapshot reconciliation yet.

## Query and export

```sh
python3 mtg_inventory.py list --name "bolt"
python3 mtg_inventory.py list --set-code m10 --location "Trade binder"
python3 mtg_inventory.py list --finish foil
python3 mtg_inventory.py export-csv /tmp/inventory-export.csv
```

Name searches match a substring. Other filters are exact and case insensitive.
`list` prints JSON, including inventory IDs and source metadata. `export-csv` supports
the same filters and exports the inventory fields for spreadsheet editing or importing
into a **fresh** database. It refuses to overwrite an existing file. CSV exports omit
internal IDs, original source rows, and import history; they are not a full database
backup, and reimporting them into the original database adds copies.

For custom queries, open the SQLite file with a database viewer or the `sqlite3` CLI:

```sql
SELECT name, set_code, finish, SUM(quantity) AS copies
FROM inventory
WHERE quantity > 0
GROUP BY name, set_code, finish
ORDER BY copies DESC;
```

For a complete backup, close inventory commands and copy `data/inventory.sqlite3`
to another location. Restore by selecting the copy with `--db`.

## Future pricing

Future price observations should live in a separate table keyed to the printing,
finish, provider, currency, and observation time. Purchase price is what you paid;
market prices change over time and should not overwrite it. That history can support
trend queries and sell-decision metrics later. Price fetching, sales tracking, and
metrics are intentionally left for subsequent steps.

## Checks

```sh
python3 -m unittest discover -s tests -v
```
