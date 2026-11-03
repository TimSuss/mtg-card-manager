# Daily prices and email alerts on the Pi

Keep the authoritative database on the Pi and use your Mac to access it over SSH.
The always-enabled systemd timer starts a short-lived price job daily. It survives
reboots, catches up after downtime, and retries failures. A loop polling constantly
would not make daily price data fresher.

The service is prepared here; it has not been installed on your Pi. SMTP credentials
and a destination address still need to be configured there. No email is sent by
ordinary price checks.

## Data source and matching

[MTGJSON publishes](https://mtgjson.com/downloads/all-files/) `AllPricesToday` for daily
updates, `AllPrices` for up to 90 days of history, and `AllIdentifiers` for printing
identification. [Prices distinguish](https://mtgjson.com/data-models/price/price-list/)
providers, currency, retail and buylist, with separate dated
[normal, foil, and etched observations](https://mtgjson.com/data-models/price/price-points/).

The tracker joins your Scryfall printing IDs to MTGJSON UUIDs using the
[identifier mapping](https://mtgjson.com/data-models/identifiers/). It prices owned
English-language copies, including each finish independently. A missing Scryfall ID,
missing price, unsupported language, or ambiguous UUID match is reported; the code
does not guess based on card names. In particular, a name's most expensive printing
cannot accidentally become the price of every copy.

Defaults use **TCGplayer paper retail reference prices, USD**. These are reference
prices from MTGJSON, not executable offers or guaranteed proceeds. There is no
trading-volume/order-book data, demand forecast, reprint/banning news feed, currency
conversion, or condition adjustment. By default alerts only consider near-mint,
unsigned, non-proxy, unaltered, non-misprint English copies. Other copies remain in
inventory. The eligible condition list is configurable, but expanding it does not
create a condition-adjusted valuation.

## Sell-review rules

`price_config.json` contains all thresholds. The initial heuristic is intentionally
explainable; it has not been backtested and makes no claim to predict the market.
Commands load the file beside the script by default; `--config` selects another file.
For each active inventory entry:

1. Require a quote no older than 3 days and at least 7 distinct prior quote dates
   inside the last 30 days. Repeated checks of the same dated feed do not create
   artificial history. An initial history import can satisfy this immediately.
2. Require at least $2 per copy and $5 estimated net proceeds for the lot.
   Net proceeds = quoted price × quantity × (1 − 15% fees) − $1.25 shipping per lot.
3. Flag if any of these rules qualifies:
   - A rise of at least 25% **and** $1 above the median of the prior observations,
     with the latest price within 5% of the window's high.
   - A unit price strictly over $12 (`median_min_unit_price`) and at least 92% of
     that historical median (`median_fraction`). The median excludes the latest
     quote and uses `lookback_days` (30 by default; set to 90 for a longer window).
     This rule can flag flat or slightly declining prices without a known purchase
     cost or proximity to the recent high. The net-proceeds filter still applies.
   - A profit target of at least $5 and 30% of recorded purchase cost after the
     same expenses. This rule requires a purchase price in the quote currency.
4. Queue a review alert containing printing, finish, box, quantity, source, quote date,
   baseline, estimated proceeds/profit, and the reasons. Your final decision still
   needs the actual condition, shipping method, selling venue, and whether you want
   to keep the card. This service never sells cards.

Change fees, shipping, minimum values, cooldown, and profit/rise thresholds to suit
your selling method. For a buylist source, choose `price_kind: "buylist"` and appropriate
costs; its history is never blended with retail. Likewise, changing provider or
currency creates a distinct series. Add box labels to `excluded_boxes` to protect
cards you want to keep, for example `["favorites", "commander-minusTokens"]`.
No boxes are excluded automatically.

Each entry is its own lot, so estimated shipping is charged to that lot. The current
algorithm does not optimize combined shipments or aggregate overlapping inventory
entries. Alerts are per lot, not a single recommendation for every copy of a card.

## Install on Raspberry Pi OS / Linux

Use a normal user account with Python 3.9+ and systemd. These instructions assume
the checkout is `~/mtg-card-manager`; edit the unit's `WorkingDirectory`,
`EnvironmentFile`, and executable path if you choose somewhere else.

First, on the Mac, refresh the inventory snapshot and commit/push the project,
`data/snapshot.sqlite3`, and `data/imports/` through your normal Git workflow:

```sh
python3 mtg_inventory.py backup
```

On the Pi, install Git and Python's virtual environment support if needed, clone your
repository to `~/mtg-card-manager`, then run:

```sh
cd ~/mtg-card-manager
cp -n data/snapshot.sqlite3 data/inventory.sqlite3
python3 mtg_inventory.py init
python3 mtg_inventory.py summary
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-prices.txt
```

Keep `data/imports/` beside the database. Once switched over, make inventory changes on
the Pi rather than independently on both machines.

## First run and local inspection

Bootstrap history once, without emailing:

```sh
.venv/bin/python mtg_prices.py --config price_config.json check --history
.venv/bin/python mtg_prices.py alerts --status pending
.venv/bin/python mtg_prices.py status
```

The first run downloads a compressed identifier file and the compressed history feed.
Allow disk space and time for these large files. `ijson` streams them; only matching
owned printing data is retained in memory and SQLite. Network downloads are cached
under `data/price-cache/` (ignored by Git). Identifiers refresh after 7 days; price
downloads refresh after 8 hours. `--refresh` bypasses the cache. The daily service
uses the smaller `AllPricesToday` feed, while SQLite retains the history you collect.
Running `check --history` again can backfill missed days within MTGJSON's history window.

The JSON report gives mapping counts, observations, skip reasons, and qualifying
signals. A successful job does not imply every card has a quote or is eligible.
Inspect the counts and `alerts` output before turning on email.

Useful commands:

```sh
# Refresh daily prices and evaluate; queue alerts without sending.
.venv/bin/python mtg_prices.py --config price_config.json check

# Reevaluate local history after changing thresholds; no downloads.
.venv/bin/python mtg_prices.py --config price_config.json evaluate

# Use previously downloaded or supplied feeds without network requests.
.venv/bin/python mtg_prices.py check --identifiers-file /path/AllIdentifiers.json.gz --prices-file /path/AllPrices.json.gz
```

Both compressed and uncompressed input files are accepted. A malformed feed is
rejected before updating history. Missing/zero prices do not overwrite known prices
with zero. Corrected prices can replace an observation for the same date. An old quote
does not become fresh just because the service downloaded it today.

## Email setup

Copy `.env.prices.example` to `.env.prices`, edit it locally on the Pi, and restrict
its permissions:

```sh
cp -n .env.prices.example .env.prices
chmod 600 .env.prices
```

Set the SMTP host/port, from/to addresses, and your mail provider's credentials.
Use an app password or SMTP credential where your provider requires it. Keep passwords
out of `price_config.json`; `.env.prices` is ignored by Git. STARTTLS on port 587 is
the default; implicit TLS (`MTG_SMTP_TLS=ssl`, usually port 465) is supported too.
Plaintext SMTP is not supported. Quote values with spaces in the environment file.

For a manual test in a shell, export the environment file and send a test message:

```sh
set -a
. ./.env.prices
set +a
.venv/bin/python mtg_prices.py test-email
```

That test checks sending independently of whether any cards currently qualify. After
reviewing pending alerts, send the digest:

```sh
.venv/bin/python mtg_prices.py --config price_config.json evaluate --send-email
```

The SQLite outbox keeps pending alerts across reboots and email failures. Retried jobs
revalidate pending signals, update their quantities/boxes, and cancel alerts that no
longer qualify (including sold cards or stale quotes). Successful delivery marks alerts
sent and starts a 7-day cooldown per entry/provider/currency/price kind/rule. The same
dated signal cannot be sent twice through normal reruns. Missing SMTP settings only
block sending, not local price collection or evaluation.

As with most SMTP outboxes, a crash after the mail server accepts a digest but before
SQLite records success can cause a duplicate on retry. Delivery is not exactly-once.
SMTP acceptance also does not guarantee inbox delivery; verify your test email.

## Enable automatic daily checks

The included units are **user services**; run `systemctl --user` as the account that
owns this checkout. After the manual check and email test succeed:

```sh
mkdir -p ~/.config/systemd/user
cp deploy/mtg-prices.service deploy/mtg-prices.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now mtg-prices.timer
sudo loginctl enable-linger "$USER"
```

Lingering lets the user service start at boot and continue after logout. The
[timer](https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html)
runs daily at 16:00 UTC plus up to 15 minutes of jitter, with catch-up for a missed
run. This is 11 AM in New York during standard time and noon during daylight time.
The service retries failures after 15 minutes, with a limited restart burst. If the
network is unavailable, it records a failed job and retries. A per-database process
lock prevents overlapping price commands; SQLite coordinates inventory writes.

Monitor or stop it with:

```sh
systemctl --user list-timers mtg-prices.timer
systemctl --user status mtg-prices.service
journalctl --user -u mtg-prices.service -n 100
systemctl --user start mtg-prices.service
systemctl --user disable --now mtg-prices.timer
systemctl --user stop mtg-prices.service
```

`disable --now` stops future timer events; stop the service separately if it is
currently running. No service has been enabled by adding these files to the repository.

## Storage and backup

`price_cards`, `price_history`, `price_alerts`, and `price_runs` are added alongside your
inventory tables. The inventory schema version stays compatible with the inventory
CLI. Prices are append-per-day/upsert-on-correction, not an overwrite of purchase cost.
Jobs record their success/failure and report in SQLite as well as stdout/journald.

Run `python3 mtg_inventory.py backup` before committing and pushing the snapshot from
the Pi. It now includes price history, job records, and sent-alert state. It does not
include SMTP credentials or download caches. A fresh restore preserves alert cooldowns
as of the snapshot. As price history grows, move database backups to dedicated backup
storage instead of routinely committing large snapshots to Git.
