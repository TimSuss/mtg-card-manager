import contextlib
import gzip
import io
import json
import os
import smtplib
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import mtg_inventory
import mtg_prices as prices


TODAY = date(2026, 11, 1)


class PriceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "inventory.sqlite3"
        self.db = prices.connect(self.path)
        self.settings = prices.load_settings()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.db.close)
        with self.db:
            mtg_inventory.insert(self.db, mtg_inventory.normalize({
                "name": "Test Card", "set_code": "tst", "collector_number": "001a",
                "scryfall_id": "sid-a", "condition": "near_mint", "quantity": 2, "box": "Box 1",
            }))

    def feed(self, name, data):
        path = self.root / name
        document = {"meta": {"date": TODAY.isoformat(), "version": "5.2.2"}, "data": data}
        with gzip.open(path, "wt", encoding="utf-8") as output:
            json.dump(document, output)
        return path

    def fixtures(self, current=20, baseline=10, finish="normal", currency="USD", days=10):
        identifiers = self.feed("identifiers.json.gz", {
            "uuid-a": {"identifiers": {"scryfallId": "sid-a"}},
            "uuid-unowned": {"identifiers": {"scryfallId": "sid-other"}},
        })
        points = {(TODAY - timedelta(days=i)).isoformat(): baseline for i in range(1, days + 1)}
        points[TODAY.isoformat()] = current
        data = {"uuid-a": {"paper": {"tcgplayer": {"currency": currency, "retail": {finish: points}}}}}
        quotes = self.feed("prices.json.gz", data)
        return identifiers, quotes

    def ingest(self, **kwargs):
        return prices.ingest(self.db, *self.fixtures(**kwargs), self.settings, TODAY)

    def evaluate(self, today=TODAY):
        return prices.evaluate(self.db, self.settings, today)

    def test_import_twice_preserves_purchase_price_and_daily_uniqueness(self):
        with self.db:
            self.db.execute("UPDATE inventory SET purchase_price='3.25',purchase_currency='USD'")
        self.ingest()
        self.ingest()
        self.assertEqual(self.db.execute("SELECT count(*) FROM price_history").fetchone()[0], 11)
        self.assertEqual(self.db.execute("SELECT purchase_price FROM inventory").fetchone()[0], "3.25")
        result = self.evaluate()
        self.assertEqual(result["new_alerts"], 1)
        signal = result["signals"][0]
        self.assertEqual(signal["estimated_net_proceeds"], "32.75")
        self.assertEqual(signal["estimated_profit"], "26.25")
        self.assertEqual(set(signal["reasons"]), {"price_rise_near_recent_high", "estimated_profit_target",
                                                "price_near_historical_median"})
        self.assertEqual(self.evaluate()["new_alerts"], 0)

    def test_low_value_flat_prices_without_known_cost_do_not_signal(self):
        self.ingest(current=4, baseline=4)
        self.assertEqual(self.evaluate()["evaluation"], {"no_signal": 1})

    def test_median_rule_boundaries_without_known_cost(self):
        for current, baseline, qualifies in (
            (20, 20, True), (18.40, 20, True), (18.39, 20, False),
            (10, 10, False), (12, 12, False), (12.01, 12, True),
        ):
            with self.subTest(current=current, baseline=baseline):
                self.ingest(current=current, baseline=baseline)
                signals = self.evaluate()["signals"]
                self.assertEqual(bool(signals), qualifies)
                if qualifies:
                    self.assertEqual(signals[0]["reasons"], ["price_near_historical_median"])

    def test_median_rule_still_requires_minimum_net_proceeds(self):
        self.ingest(current=13, baseline=13)
        self.settings["shipping_per_lot"] = prices.money("10")
        with self.db:
            self.db.execute("UPDATE inventory SET quantity=1")
        self.assertEqual(self.evaluate()["evaluation"], {"below_value_threshold": 1})

    def test_profit_target_requires_matching_purchase_currency(self):
        self.ingest(current=10, baseline=20)
        with self.db:
            self.db.execute("UPDATE inventory SET purchase_price='1', purchase_currency='EUR'")
        self.assertEqual(self.evaluate()["signals"], [])
        with self.db:
            self.db.execute("UPDATE inventory SET purchase_currency='USD'")
        signal = self.evaluate()["signals"][0]
        self.assertEqual(signal["reasons"], ["estimated_profit_target"])

    def test_low_value_spikes_are_filtered_after_fees(self):
        self.ingest(current=2, baseline=0.5)
        self.assertEqual(self.evaluate()["evaluation"], {"below_value_threshold": 1})

    def test_insufficient_history_and_stale_prices_do_not_alert(self):
        self.ingest(days=2)
        self.assertEqual(self.evaluate()["evaluation"], {"insufficient_history": 1})
        self.ingest()
        self.assertEqual(self.evaluate(TODAY + timedelta(days=4))["evaluation"], {"stale_price": 1})

    def test_separate_finishes_and_no_name_based_matching(self):
        self.ingest(finish="foil")
        self.assertEqual(self.evaluate()["evaluation"], {"missing_price": 1})
        with self.db:
            self.db.execute("UPDATE inventory SET finish='foil'")
        self.assertEqual(len(self.evaluate()["signals"]), 1)
        with self.db:
            self.db.execute("UPDATE inventory SET scryfall_id='another-printing'")
        self.assertEqual(self.evaluate()["evaluation"], {"missing_price": 1})

    def test_ambiguous_identifiers_are_reported(self):
        self.ingest()
        with self.db:
            self.db.execute("INSERT INTO price_cards VALUES ('uuid-b','sid-a',1)")
            self.db.execute("INSERT INTO price_history SELECT 'uuid-b',provider,currency,price_kind,"
                            "finish,price_date,amount,fetched_at FROM price_history WHERE uuid='uuid-a'")
        self.assertEqual(self.evaluate()["evaluation"], {"ambiguous_printing": 1})

    def test_invalid_or_currency_mismatched_feed_preserves_previous_history(self):
        self.ingest()
        before = self.db.execute("SELECT * FROM price_history ORDER BY price_date").fetchall()
        with self.assertRaisesRegex(ValueError, "No matching prices"):
            self.ingest(currency="EUR")
        self.assertEqual(before, self.db.execute("SELECT * FROM price_history ORDER BY price_date").fetchall())
        with self.assertRaisesRegex(ValueError, "nonnegative"):
            self.ingest(current=-1)
        self.assertEqual(before, self.db.execute("SELECT * FROM price_history ORDER BY price_date").fetchall())

    def test_truncated_json_does_not_partially_import(self):
        identifiers, quotes = self.fixtures()
        broken = self.root / "broken.json"
        broken.write_text('{"data":{"uuid-a":{"paper":{"tcgplayer":{"currency":"USD",'
                          '"retail":{"normal":{"2026-11-01":20}}}}},"another":')
        with self.assertRaises(Exception):
            prices.ingest(self.db, identifiers, broken, self.settings, TODAY)
        self.assertEqual(self.db.execute("SELECT count(*) FROM price_history").fetchone()[0], 0)

    def test_proxy_language_condition_and_box_exclusions(self):
        self.ingest()
        for field, value, reason in (
            ("raw_json", '{"Proxy":"true"}', "special_copy"),
            ("raw_json", '{"Signed":"true"}', "special_copy"),
            ("language", "ja", "unsupported_language"),
            ("condition", "damaged", "excluded_condition"),
        ):
            with self.subTest(field=field, value=value):
                with self.db:
                    self.db.execute("UPDATE inventory SET raw_json='{}', language='en',condition='near_mint'")
                    self.db.execute("UPDATE inventory SET " + field + "=?", (value,))
                self.assertEqual(self.evaluate()["evaluation"], {reason: 1})
        with self.db:
            self.db.execute("UPDATE inventory SET condition='near_mint'")
        self.settings["excluded_boxes"] = ["box 1"]
        self.assertEqual(self.evaluate()["evaluation"], {"excluded_box": 1})

    def test_pending_alerts_cancel_when_sold_or_stale(self):
        self.ingest()
        self.evaluate()
        with self.db:
            self.db.execute("UPDATE inventory SET quantity=0")
        self.evaluate()
        self.assertEqual(self.db.execute("SELECT status FROM price_alerts").fetchone()[0], "cancelled")
        with self.db:
            self.db.execute("UPDATE inventory SET quantity=2")
        self.evaluate()
        self.assertEqual(self.db.execute("SELECT status FROM price_alerts").fetchone()[0], "pending")
        self.evaluate(TODAY + timedelta(days=4))
        self.assertEqual(self.db.execute("SELECT status FROM price_alerts").fetchone()[0], "cancelled")

    def test_sent_alert_cooldown_and_same_date_deduplication(self):
        self.ingest()
        self.evaluate()
        with self.db:
            self.db.execute("UPDATE price_alerts SET status='sent', sent_at='2026-11-01T12:00:00Z'")
        self.assertEqual(self.evaluate()["new_alerts"], 0)
        with self.db:
            self.db.execute("INSERT INTO price_history SELECT uuid,provider,currency,price_kind,finish,"
                            "'2026-11-02',amount,fetched_at FROM price_history WHERE price_date='2026-11-01'")
        self.assertEqual(self.evaluate(TODAY + timedelta(days=1))["new_alerts"], 0)
        self.settings["cooldown_days"] = 0
        self.assertEqual(self.evaluate(TODAY + timedelta(days=1))["new_alerts"], 1)

    def test_smtp_failure_retries_pending_and_success_marks_sent(self):
        self.ingest()
        self.evaluate()
        env = {"MTG_SMTP_HOST": "smtp.example.com", "MTG_SMTP_FROM": "from@example.com",
               "MTG_SMTP_TO": "to@example.com", "MTG_SMTP_USER": "user", "MTG_SMTP_PASSWORD": "secret"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(prices.smtplib, "SMTP") as smtp:
            connection = smtp.return_value.__enter__.return_value
            connection.send_message.side_effect = smtplib.SMTPException("temporary failure")
            with self.assertRaises(smtplib.SMTPException):
                prices.deliver(self.db, self.settings)
            row = self.db.execute("SELECT status,attempts,last_error FROM price_alerts").fetchone()
            self.assertEqual(row["status"], "pending")
            self.assertEqual(row["attempts"], 1)
            self.assertNotIn("secret", row["last_error"])
            connection.send_message.side_effect = None
            connection.send_message.return_value = {}
            self.assertEqual(prices.deliver(self.db, self.settings), 1)
            connection.starttls.assert_called()
            self.assertIn("Box 1", str(connection.send_message.call_args[0][0]))
            self.assertEqual(self.db.execute("SELECT status FROM price_alerts").fetchone()[0], "sent")
            self.assertEqual(prices.deliver(self.db, self.settings), 0)

    def test_smtp_configuration_is_required_only_when_delivering(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(prices.deliver(self.db, self.settings), 0)
            self.ingest()
            self.evaluate()
            with self.assertRaisesRegex(ValueError, "MTG_SMTP_HOST"):
                prices.deliver(self.db, self.settings)
        self.assertEqual(self.db.execute("SELECT status FROM price_alerts").fetchone()[0], "pending")

    def test_email_test_uses_tls_and_does_not_create_sell_alerts(self):
        env = {"MTG_SMTP_HOST": "smtp.example.com", "MTG_SMTP_FROM": "from@example.com",
               "MTG_SMTP_TO": "to@example.com", "MTG_SMTP_TLS": "ssl"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(prices.smtplib, "SMTP_SSL") as smtp:
            connection = smtp.return_value.__enter__.return_value
            connection.send_message.return_value = {}
            prices.test_email()
            self.assertIn("email test", connection.send_message.call_args[0][0]["Subject"])
            self.assertEqual(smtp.call_args[0][1], 465)
        self.assertEqual(self.db.execute("SELECT count(*) FROM price_alerts").fetchone()[0], 0)

    def test_lock_prevents_overlapping_jobs(self):
        with prices.process_lock(self.path):
            with self.assertRaisesRegex(ValueError, "Another price command"):
                with prices.process_lock(self.path):
                    self.fail("Should not acquire the lock")

    def test_cli_offline_check_reports_failures_and_can_read_status(self):
        identifiers, quotes = self.fixtures()
        with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(prices, "evaluate", wraps=prices.evaluate) as evaluate:
            self.assertEqual(prices.main(["--db", str(self.path), "check", "--identifiers-file", str(identifiers),
                                          "--prices-file", str(quotes)]), 0)
            evaluate.assert_called_once()
            self.assertEqual(prices.main(["--db", str(self.path), "status"]), 0)
        broken = self.root / "broken.json"
        broken.write_text("not json")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(prices.main(["--db", str(self.path), "check", "--identifiers-file", str(broken),
                                          "--prices-file", str(quotes)]), 1)
        last = self.db.execute("SELECT * FROM price_runs ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(last["status"], "failed")

    def test_snapshot_includes_history_and_outbox(self):
        self.ingest()
        self.evaluate()
        target = mtg_inventory.backup_database(self.db)
        with contextlib.closing(sqlite3.connect(str(target))) as snapshot:
            self.assertEqual(snapshot.execute("SELECT count(*) FROM price_history").fetchone()[0], 11)
            self.assertEqual(snapshot.execute("SELECT count(*) FROM price_alerts").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
