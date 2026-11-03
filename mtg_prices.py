"""Daily MTGJSON prices, explainable sell-review signals, and an email outbox."""

import argparse
import fcntl
import gzip
import json
import os
import shutil
import smtplib
import sqlite3
import ssl
import sys
import tempfile
from collections import Counter, defaultdict
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.message import EmailMessage
from pathlib import Path
from statistics import median
from urllib.request import Request, urlopen

import mtg_inventory


BASE_URL = "https://mtgjson.com/api/v5/"
RULE = "sell-review-v1"
DEFAULTS = {
    "provider": "tcgplayer", "currency": "USD", "price_kind": "retail",
    "lookback_days": 30, "min_history_days": 7, "max_age_days": 3,
    "rise_fraction": "0.25", "min_rise_amount": "1.00", "near_high_fraction": "0.95",
    "median_min_unit_price": "12.00", "median_fraction": "0.92",
    "fee_fraction": "0.15", "shipping_per_lot": "1.25", "min_unit_price": "2.00",
    "min_net_proceeds": "5.00", "min_profit": "5.00", "profit_fraction": "0.30",
    "cooldown_days": 7, "excluded_boxes": [], "eligible_conditions": ["near_mint", "nm"],
}
DECIMAL_KEYS = {key for key, value in DEFAULTS.items() if isinstance(value, str)
                and key not in ("provider", "currency", "price_kind")}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def load_settings(path=None):
    settings = dict(DEFAULTS)
    supplied = json.loads(Path(path).read_text()) if path else {}
    if not isinstance(supplied, dict) or set(supplied) - set(DEFAULTS):
        raise ValueError("Price config must be an object with only documented settings")
    settings.update(supplied)
    for key in DECIMAL_KEYS:
        settings[key] = money(settings[key])
    for key in ("lookback_days", "min_history_days", "max_age_days", "cooldown_days"):
        value = settings[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(key + " must be a nonnegative integer")
    if not 2 <= settings["min_history_days"] < settings["lookback_days"]:
        raise ValueError("min_history_days must be at least 2 and below lookback_days")
    if settings["fee_fraction"] >= 1 or not 0 < settings["near_high_fraction"] <= 1:
        raise ValueError("fee_fraction must be below 1; near_high_fraction must be in (0,1]")
    if not 0 < settings["median_fraction"] <= 1:
        raise ValueError("median_fraction must be in (0,1]")
    if settings["provider"] not in ("tcgplayer", "cardkingdom", "cardmarket", "cardsphere", "manapool"):
        raise ValueError("Unsupported MTGJSON paper price provider")
    if settings["price_kind"] not in ("retail", "buylist"):
        raise ValueError("price_kind must be retail or buylist")
    if not isinstance(settings["currency"], str) or len(settings["currency"]) != 3:
        raise ValueError("currency must be a three-letter code")
    settings["currency"] = settings["currency"].upper()
    for key in ("excluded_boxes", "eligible_conditions"):
        if not isinstance(settings[key], list) or not all(isinstance(v, str) for v in settings[key]):
            raise ValueError(key + " must be a list of strings")
    return settings


def money(value):
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("Invalid price or numeric setting")
    if not amount.is_finite() or amount < 0:
        raise ValueError("Prices and numeric settings must be finite and nonnegative")
    return amount


def connect(path):
    db = mtg_inventory.connect(path)
    db.execute("PRAGMA busy_timeout = 30000")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS price_cards (
            uuid TEXT PRIMARY KEY, scryfall_id TEXT NOT NULL, active INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS price_cards_scryfall ON price_cards(scryfall_id);
        CREATE TABLE IF NOT EXISTS price_history (
            uuid TEXT NOT NULL, provider TEXT NOT NULL, currency TEXT NOT NULL,
            price_kind TEXT NOT NULL, finish TEXT NOT NULL, price_date TEXT NOT NULL,
            amount TEXT NOT NULL, fetched_at TEXT NOT NULL,
            PRIMARY KEY (uuid, provider, currency, price_kind, finish, price_date)
        );
        CREATE TABLE IF NOT EXISTS price_alerts (
            id INTEGER PRIMARY KEY, inventory_id INTEGER NOT NULL REFERENCES inventory(id),
            provider TEXT NOT NULL, currency TEXT NOT NULL, price_kind TEXT NOT NULL,
            price_date TEXT NOT NULL, rule TEXT NOT NULL, payload TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','sent','cancelled')),
            created_at TEXT NOT NULL, sent_at TEXT, attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            UNIQUE(inventory_id, provider, currency, price_kind, price_date, rule)
        );
        CREATE TABLE IF NOT EXISTS price_runs (
            id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
            command TEXT NOT NULL, status TEXT NOT NULL, report TEXT, error TEXT
        );
    """)
    return db


@contextmanager
def process_lock(database):
    with Path(str(database) + ".prices.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another price command is running for this database")
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def download(filename, cache, max_age_hours, refresh=False):
    """Stream to disk; interrupted downloads leave the previous cached file intact."""
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / filename
    if target.exists() and not refresh:
        age = datetime.now(timezone.utc).timestamp() - target.stat().st_mtime
        if 0 <= age < max_age_hours * 3600:
            return target
    request = Request(BASE_URL + filename, headers={
        "User-Agent": "mtg-card-manager/1.0 (personal collection price tracker)",
        "Accept": "application/gzip, application/json",
    })
    descriptor, temporary = tempfile.mkstemp(prefix=".download-", dir=cache)
    temporary = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as output, urlopen(request, timeout=120) as response:
            shutil.copyfileobj(response, output, length=1024 * 1024)
        # Ensure gzip headers are readable before replacing a previous cache.
        with gzip.open(temporary, "rb") as stream:
            stream.read(1)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def stream_data(path):
    try:
        import ijson
    except ImportError:
        raise ValueError("Install pricing dependencies: python -m pip install -r requirements-prices.txt")
    opener = gzip.open if str(path).lower().endswith(".gz") else open
    count = 0
    with opener(path, "rb") as stream:
        for key, value in ijson.kvitems(stream, "data"):
            count += 1
            if not isinstance(value, dict):
                raise ValueError("MTGJSON data entries must be objects")
            yield key, value
    if not count:
        raise ValueError("MTGJSON file has no data entries")


def ingest(db, identifiers, prices, settings, today=None):
    today = today or datetime.now(timezone.utc).date()
    owned = {row[0] for row in db.execute(
        "SELECT DISTINCT scryfall_id FROM inventory WHERE quantity > 0 AND scryfall_id != '' "
        "AND lower(language) IN ('en','english')")}
    mappings = {}
    for uuid, card in stream_data(identifiers):
        sid = card.get("identifiers", {}).get("scryfallId")
        if sid in owned and not card.get("isOnlineOnly", False):
            mappings[uuid] = sid
    if owned and not mappings:
        raise ValueError("No owned Scryfall IDs matched MTGJSON; inventory was not repriced")
    observations = []
    priced_ids = set()
    for uuid, card_prices in stream_data(prices):
        if uuid not in mappings:
            continue
        provider = card_prices.get("paper", {}).get(settings["provider"], {})
        if provider.get("currency", "").upper() != settings["currency"]:
            continue
        for finish, values in provider.get(settings["price_kind"], {}).items():
            if finish not in ("normal", "foil", "etched"):
                continue
            for day, value in values.items():
                observation_date = date.fromisoformat(day)
                if observation_date > today or value is None:
                    continue
                amount = money(value)
                if amount == 0:
                    continue
                observations.append((uuid, settings["provider"], settings["currency"],
                                     settings["price_kind"], "nonfoil" if finish == "normal" else finish,
                                     day, str(amount), utc_now()))
                priced_ids.add(mappings[uuid])
    if owned and not observations:
        raise ValueError("No matching prices in the selected feed/provider/currency; history unchanged")
    with db:
        db.execute("UPDATE price_cards SET active = 0")
        db.executemany("INSERT INTO price_cards VALUES (?, ?, 1) ON CONFLICT(uuid) "
                       "DO UPDATE SET scryfall_id=excluded.scryfall_id, active=1", mappings.items())
        db.executemany("INSERT INTO price_history VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                       "ON CONFLICT(uuid,provider,currency,price_kind,finish,price_date) DO UPDATE "
                       "SET amount=excluded.amount, fetched_at=excluded.fetched_at "
                       "WHERE amount != excluded.amount", observations)
    return {"owned_scryfall_ids": len(owned), "matched_scryfall_ids": len(set(mappings.values())),
            "ids_with_prices": len(priced_ids), "feed_observations": len(observations)}


def flag(value):
    return str(value).strip().lower() in ("true", "yes", "1")


def signal_for(db, entry, settings, today):
    if entry["box"].casefold() in {b.casefold() for b in settings["excluded_boxes"]}:
        return None, "excluded_box"
    raw = json.loads(entry["raw_json"])
    if entry["altered"] or entry["misprint"] or any(
        flag(value) for key, value in raw.items() if key.lower() in ("proxy", "signed")
    ):
        return None, "special_copy"
    if entry["language"].lower() not in ("en", "english"):
        return None, "unsupported_language"
    if entry["condition"].casefold() not in {c.casefold() for c in settings["eligible_conditions"]}:
        return None, "excluded_condition"
    if not entry["scryfall_id"]:
        return None, "missing_scryfall_id"
    params = (entry["scryfall_id"], settings["provider"], settings["currency"],
              settings["price_kind"], entry["finish"], today.isoformat())
    candidates = db.execute(
        "SELECT DISTINCT c.uuid FROM price_cards c JOIN price_history h ON h.uuid=c.uuid "
        "WHERE c.active=1 AND c.scryfall_id=? AND h.provider=? AND h.currency=? "
        "AND h.price_kind=? AND h.finish=? AND h.price_date<=?", params).fetchall()
    if len(candidates) != 1:
        return None, "ambiguous_printing" if candidates else "missing_price"
    rows = db.execute(
        "SELECT price_date,amount FROM price_history WHERE uuid=? AND provider=? AND currency=? "
        "AND price_kind=? AND finish=? AND price_date<=? AND price_date>=? ORDER BY price_date",
        (candidates[0][0], *params[1:], (today - timedelta(days=settings["lookback_days"])).isoformat()),
    ).fetchall()
    if not rows or (today - date.fromisoformat(rows[-1][0])).days > settings["max_age_days"]:
        return None, "stale_price"
    if len(rows) - 1 < settings["min_history_days"]:
        return None, "insufficient_history"
    current = money(rows[-1][1])
    baseline = median([money(row[1]) for row in rows[:-1]])
    high = max(money(row[1]) for row in rows)
    quantity = entry["quantity"]
    net = current * quantity * (1 - settings["fee_fraction"]) - settings["shipping_per_lot"]
    if current < settings["min_unit_price"] or net < settings["min_net_proceeds"]:
        return None, "below_value_threshold"
    rise = current / baseline - 1
    reasons = []
    if (rise >= settings["rise_fraction"] and current - baseline >= settings["min_rise_amount"]
            and current >= high * settings["near_high_fraction"]):
        reasons.append("price_rise_near_recent_high")
    if (current > settings["median_min_unit_price"]
            and current >= baseline * settings["median_fraction"]):
        reasons.append("price_near_historical_median")
    profit = None
    if entry["purchase_price"] and entry["purchase_currency"].upper() == settings["currency"]:
        cost = money(entry["purchase_price"]) * quantity
        profit = net - cost
        if profit >= settings["min_profit"] and profit >= cost * settings["profit_fraction"]:
            reasons.append("estimated_profit_target")
    if not reasons:
        return None, "no_signal"
    return {
        "inventory_id": entry["id"], "name": entry["name"], "set_code": entry["set_code"],
        "collector_number": entry["collector_number"], "finish": entry["finish"],
        "condition": entry["condition"], "box": entry["box"], "quantity": quantity,
        "provider": settings["provider"], "currency": settings["currency"],
        "price_kind": settings["price_kind"], "price_date": rows[-1][0], "unit_price": str(current),
        "baseline_median": str(baseline), "history_days": len(rows),
        "rise_fraction": str(rise.quantize(Decimal("0.0001"))),
        "estimated_net_proceeds": str(net.quantize(Decimal("0.01"))),
        "estimated_profit": str(profit.quantize(Decimal("0.01"))) if profit is not None else None,
        "reasons": reasons, "rule": RULE,
    }, "signal"


def evaluate(db, settings, today=None):
    today = today or datetime.now(timezone.utc).date()
    counts, signals = Counter(), {}
    for entry in db.execute("SELECT * FROM inventory WHERE quantity>0"):
        signal, reason = signal_for(db, entry, settings, today)
        counts[reason] += 1
        if signal:
            signals[entry["id"]] = signal
    scope = (settings["provider"], settings["currency"], settings["price_kind"], RULE)
    queued = 0
    with db:
        pending = db.execute("SELECT * FROM price_alerts WHERE provider=? AND currency=? "
                             "AND price_kind=? AND rule=? AND status='pending'", scope).fetchall()
        pending_ids = set()
        for row in pending:
            signal = signals.get(row["inventory_id"])
            if signal:
                db.execute("UPDATE price_alerts SET payload=? WHERE id=?", (json.dumps(signal), row["id"]))
                pending_ids.add(row["inventory_id"])
            else:
                db.execute("UPDATE price_alerts SET status='cancelled' WHERE id=?", (row["id"],))
        for entry_id, signal in signals.items():
            if entry_id in pending_ids:
                continue
            cutoff = (today - timedelta(days=settings["cooldown_days"])).isoformat()
            recent = db.execute("SELECT 1 FROM price_alerts WHERE inventory_id=? AND provider=? "
                                "AND currency=? AND price_kind=? AND rule=? AND status='sent' "
                                "AND substr(sent_at,1,10)>?", (entry_id, *scope, cutoff)).fetchone()
            if recent:
                continue
            changed = db.execute(
                "INSERT INTO price_alerts (inventory_id,provider,currency,price_kind,price_date,rule,"
                "payload,status,created_at) VALUES (?,?,?,?,?,?,?,'pending',?) "
                "ON CONFLICT(inventory_id,provider,currency,price_kind,price_date,rule) DO UPDATE "
                "SET status='pending',payload=excluded.payload WHERE status='cancelled'",
                (entry_id, *scope[:3], signal["price_date"], RULE, json.dumps(signal), utc_now()),
            ).rowcount
            queued += changed
    return {"evaluation": dict(counts), "new_alerts": queued, "signals": list(signals.values())}


def email_settings():
    names = ("HOST", "FROM", "TO")
    result = {key.lower(): os.environ.get("MTG_SMTP_" + key, "") for key in names}
    if not all(result.values()):
        raise ValueError("Email requires MTG_SMTP_HOST, MTG_SMTP_FROM, MTG_SMTP_TO; see .env.prices.example")
    result["tls"] = os.environ.get("MTG_SMTP_TLS", "starttls")
    if result["tls"] not in ("starttls", "ssl"):
        raise ValueError("MTG_SMTP_TLS must be starttls or ssl")
    result["port"] = int(os.environ.get("MTG_SMTP_PORT", "465" if result["tls"] == "ssl" else "587"))
    result["user"] = os.environ.get("MTG_SMTP_USER", "")
    result["password"] = os.environ.get("MTG_SMTP_PASSWORD", "")
    if bool(result["user"]) != bool(result["password"]):
        raise ValueError("Set both MTG_SMTP_USER and MTG_SMTP_PASSWORD for authentication")
    return result


def send_email(message, smtp):
    context = ssl.create_default_context()
    client = smtplib.SMTP_SSL if smtp["tls"] == "ssl" else smtplib.SMTP
    kwargs = {"context": context} if smtp["tls"] == "ssl" else {}
    with client(smtp["host"], smtp["port"], timeout=30, **kwargs) as connection:
        if smtp["tls"] == "starttls":
            connection.ehlo()
            connection.starttls(context=context)
            connection.ehlo()
        if smtp["user"]:
            connection.login(smtp["user"], smtp["password"])
        refused = connection.send_message(message)
        if refused:
            raise ValueError("Email rejected for one or more recipients")


def test_email():
    smtp = email_settings()
    message = EmailMessage()
    message["Subject"] = "[MTG inventory] Price tracker email test"
    message["From"] = smtp["from"]
    message["To"] = smtp["to"]
    message.set_content("The MTG inventory price tracker can send email from this machine.")
    send_email(message, smtp)


def deliver(db, settings):
    rows = db.execute("SELECT * FROM price_alerts WHERE status='pending' AND provider=? "
                      "AND currency=? AND price_kind=? AND rule=? ORDER BY id",
                      (settings["provider"], settings["currency"], settings["price_kind"], RULE)).fetchall()
    if not rows:
        return 0
    smtp = email_settings()
    message = EmailMessage()
    message["Subject"] = "[MTG inventory] {} cards to review for selling".format(len(rows))
    message["From"] = smtp["from"]
    message["To"] = smtp["to"]
    lines = ["Sell-review signals, not guaranteed sale prices or predictions.",
             "Estimates use your configured fees/shipping; quotes are not condition-adjusted.", ""]
    for row in rows:
        item = json.loads(row["payload"])
        lines.extend([
            "#{inventory_id}: {name} ({set_code} #{collector_number}, {finish})".format(**item),
            "Box: {box}; copies: {quantity}; quote date: {price_date}".format(**item),
            "{provider} {price_kind}: {unit_price} {currency}/copy; median: {baseline_median}".format(**item),
            "Estimated net for lot: {estimated_net_proceeds} {currency}; profit: {estimated_profit}".format(**item),
            "Reasons: " + ", ".join(item["reasons"]), "",
        ])
    message.set_content("\n".join(lines))
    ids = [row["id"] for row in rows]
    with db:
        db.executemany("UPDATE price_alerts SET attempts=attempts+1 WHERE id=?", [(i,) for i in ids])
    try:
        send_email(message, smtp)
    except Exception:
        with db:
            db.executemany("UPDATE price_alerts SET last_error='SMTP delivery failed; retry pending' WHERE id=?",
                           [(i,) for i in ids])
        raise
    with db:
        db.executemany("UPDATE price_alerts SET status='sent',sent_at=?,last_error=NULL WHERE id=?",
                       [(utc_now(), i) for i in ids])
    return len(rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/inventory.sqlite3")
    parser.add_argument("--config", default=str(Path(__file__).with_name("price_config.json")),
                        help="JSON settings; defaults to price_config.json beside this script")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="download feeds, update history, evaluate sell signals")
    check.add_argument("--history", action="store_true", help="fetch AllPrices (90 days) instead of daily feed")
    check.add_argument("--refresh", action="store_true", help="bypass download cache")
    check.add_argument("--identifiers-file", help="use local AllIdentifiers JSON[.gz]")
    check.add_argument("--prices-file", help="use local AllPrices[Today] JSON[.gz]")
    check.add_argument("--send-email", action="store_true")
    analyze = commands.add_parser("evaluate", help="evaluate saved history and queue alerts without downloading")
    analyze.add_argument("--send-email", action="store_true")
    alerts = commands.add_parser("alerts", help="inspect the durable email outbox")
    alerts.add_argument("--status", choices=("pending", "sent", "cancelled"))
    commands.add_parser("status", help="show last job and database counts")
    commands.add_parser("test-email", help="send a test message using SMTP environment settings")
    args = parser.parse_args(argv)
    db = None
    run_id = None
    try:
        if not Path(args.db).is_file():
            raise ValueError("Inventory database not found; restore or import your inventory first")
        settings = load_settings(args.config)
        with process_lock(args.db), closing(connect(args.db)) as db:
            if args.command == "alerts":
                sql = "SELECT * FROM price_alerts"
                rows = db.execute(sql + (" WHERE status=?" if args.status else "") + " ORDER BY id DESC",
                                  (args.status,) if args.status else ()).fetchall()
                result = [dict(row) for row in rows]
                for row in result:
                    row["payload"] = json.loads(row["payload"])
                print(json.dumps(result, indent=2))
                return 0
            if args.command == "status":
                last = db.execute("SELECT * FROM price_runs ORDER BY id DESC LIMIT 1").fetchone()
                print(json.dumps({"last_run": dict(last) if last else None,
                                  "price_observations": db.execute("SELECT count(*) FROM price_history").fetchone()[0],
                                  "pending_alerts": db.execute("SELECT count(*) FROM price_alerts WHERE status='pending'").fetchone()[0]}, indent=2))
                return 0
            if args.command == "test-email":
                test_email()
                print("Test email accepted by SMTP server; check your inbox.")
                return 0
            with db:
                run_id = db.execute("INSERT INTO price_runs (started_at,command,status) VALUES (?,?,'running')",
                                    (utc_now(), args.command)).lastrowid
            try:
                report = {}
                if args.command == "check":
                    if not db.execute("SELECT 1 FROM inventory WHERE quantity>0 LIMIT 1").fetchone():
                        raise ValueError("No owned cards; import an inventory before fetching prices")
                    cache = Path(args.db).parent / "price-cache"
                    identifiers = args.identifiers_file or download("AllIdentifiers.json.gz", cache, 168, args.refresh)
                    filename = "AllPrices.json.gz" if args.history else "AllPricesToday.json.gz"
                    prices = args.prices_file or download(filename, cache, 8, args.refresh)
                    report.update(ingest(db, identifiers, prices, settings))
                report.update(evaluate(db, settings))
                if args.send_email:
                    report["emailed_alerts"] = deliver(db, settings)
                with db:
                    db.execute("UPDATE price_runs SET finished_at=?,status='ok',report=? WHERE id=?",
                               (utc_now(), json.dumps(report), run_id))
                print(json.dumps(report, indent=2))
                return 0
            except Exception as exc:
                with db:
                    db.execute("UPDATE price_runs SET finished_at=?,status='failed',error=? WHERE id=?",
                               (utc_now(), type(exc).__name__ + ": " + str(exc), run_id))
                raise
    except Exception as exc:
        print("Price check failed: {}: {}".format(type(exc).__name__, exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
