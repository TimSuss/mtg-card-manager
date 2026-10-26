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
Commands create the database if needed (except `backup`, which requires an existing
database). The live database is ignored by Git; archived CSVs and a backup snapshot
are included as described below.

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

## Import CSVs and archive the originals

Put CSVs in `data/inbox/` (`init` creates this folder), then preview and import all of
them together:

```sh
python3 mtg_inventory.py import-csv --dry-run
python3 mtg_inventory.py import-csv
```

You can also specify a folder or several individual files:

```sh
python3 mtg_inventory.py import-csv /path/to/manabox.csv --dry-run
python3 mtg_inventory.py import-csv favorites.csv "box 1.csv" "box 2.csv"
python3 mtg_inventory.py import-csv /path/to/csv-folder
```

Folders include their immediate `.csv` files (case insensitive), processed in filename
order; subfolders are not scanned. The default inbox is beside the selected database,
so `--db /path/to/collection.sqlite3` uses `/path/to/inbox/`.

The preview reports entries and total copies per file and overall without adding cards
or writing archives. Identical files within a batch count only once, including in a
preview. Each file is a separate transaction: all of its rows must validate before any
are added. Errors report CSV line numbers. A bad file is reported and other files
continue; the command exits with a failure status if any files failed. Successful files
stay imported, so retrying a folder is safe. UTF-8 CSVs
with or without a byte order mark are supported, including quoted commas in names.
Files must use commas as separators and periods in decimal prices.

Successful imports save an exact, byte-for-byte copy in `data/imports/`, beside the
database, named `<full-SHA256-hash>_<original-filename>`. Existing archives are verified
and never overwritten. If an archive cannot be saved, that file's inventory import is
rolled back. Source CSVs stay in place; after success, you may remove them from the
inbox. Keeping them there is also safe because repeat imports are skipped.

```text
data/
  inventory.sqlite3
  inbox/               # CSVs ready to import
  imports/             # archived originals; retain these
  backups/             # database backups, when created
```

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
Every imported entry links to its import batch. Card listings and CSV exports show
`source_filename` (for example, `favorites.csv`), `source_row`, `imported_at`, and
`source_sha256`, and `source_archive` directly. Archive paths are relative to the
database folder, so moving the database and `imports/` together preserves the links.
These remain attached when you edit an entry and do not
depend on the original file still existing. Manually added entries have no import
source. Existing databases are upgraded automatically. Re-running a previously imported
file archives its original bytes if needed without adding inventory again.

**Imports add inventory; they do not synchronize a collection snapshot.** Importing
the exact same file again is skipped, even under a different filename. Changed files
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
internal IDs, original row contents, and full import history; they are not a full
database backup, and reimporting them into the original database adds copies.
The source columns in exports are informational: reimported entries link to the new
import file, with the previous source columns preserved in their original row JSON.

For custom queries, open the SQLite file with a database viewer or the `sqlite3` CLI:

```sql
SELECT name, set_code, finish, SUM(quantity) AS copies
FROM inventory
WHERE quantity > 0
GROUP BY name, set_code, finish
ORDER BY copies DESC;
```

## Back up to GitHub and restore

Git tracks `data/imports/` (the original CSVs) and `data/snapshot.sqlite3` (a database
snapshot). The live database, inbox, temporary SQLite files, and local `data/backups/`
remain ignored. CSVs alone cannot recover manual edits or future price history; the
snapshot preserves the entire database, including those changes and import records.

After changing inventory, refresh the snapshot **before committing and pushing**:

```sh
python3 mtg_inventory.py backup
git add data/snapshot.sqlite3 data/imports
git commit -m "Back up collection"
git push
```

The command uses SQLite's backup API to capture committed data consistently even while
other connections are active, checks the result, and atomically replaces the previous
snapshot. It never copies the working database file directly. Snapshot creation is
manual: edits after the last `backup` are not included until you run it again. A failed
refresh preserves the previous snapshot. Commit code changes through your usual workflow
as well so a fresh checkout contains the inventory tool.

On a new machine, clone the repository and restore the snapshot **before** importing
or adding anything:

```sh
cp -n data/snapshot.sqlite3 data/inventory.sqlite3
python3 mtg_inventory.py init
python3 mtg_inventory.py summary
```

`cp -n` avoids overwriting an existing inventory. Keep the tracked `data/imports/`
folder beside the restored database; archive links are relative and continue to work.
You do not need to reimport the CSVs. This restores the state of the last committed
and pushed snapshot. GitHub protects only data you have actually pushed.

When the Pi becomes the primary host, run `backup` there and push from there. Treat
database snapshots as complete files, not mergeable changes: keep one authoritative
writer rather than independently updating collections on both machines. If future
price history makes snapshots large or frequently changing, move database backups to
dedicated backup storage; archived CSVs can stay in Git.

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
