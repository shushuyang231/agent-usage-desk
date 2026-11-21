import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import zstandard

from engine import VERSION, IncompleteRebuild, UsageIndex
from pricing import price_for
from readers import discover_codex, read_antigravity, read_codex


DAY = "2026-11-21"
STAMP = DAY + "T12:00:00"


def token_count(model, input_tokens, cached, output, written=0):
    usage = {"input_tokens": input_tokens, "cached_input_tokens": cached,
             "output_tokens": output, "cache_write_input_tokens": written}
    return {"type": "event_msg", "timestamp": STAMP, "payload": {"type": "token_count", "info": {
        "model": model, "last_token_usage": usage, "total_token_usage": usage,
    }}}


def rollout(root, name, *records):
    path = root / "sessions" / DAY / ("rollout-" + name + ".jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(map(json.dumps, records)) + "\n", encoding="utf-8")
    return path


def varint(value):
    encoded = bytearray()
    while value >= 128:
        encoded.append(value & 127 | 128)
        value >>= 7
    return bytes(encoded) + bytes([value])


def int_field(key, value):
    return varint(key * 8) + varint(value)


def blob_field(key, data):
    return varint(key * 8 + 2) + varint(len(data)) + data


def antigravity_db(root):
    path = root / "antigravity-ide" / "conversations" / "synthetic.db"
    path.parent.mkdir(parents=True)
    usage = b"".join(int_field(key, value) for key, value in ((2, 100), (3, 50), (5, 1000), (9, 20), (10, 30)))
    usage += blob_field(7, b"synthetic-response")
    generation = blob_field(4, usage) + blob_field(17, blob_field(2, usage))
    generation += blob_field(19, b"gemini-flash-worker-agy") + blob_field(21, b"Gemini 4 Hydrogen (Orchestrator)")
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE gen_metadata (idx INTEGER, data BLOB)")
        db.executemany("INSERT INTO gen_metadata VALUES (?,?)", [(i, blob_field(1, generation)) for i in (1, 2)])
    return path


class PricingTest(unittest.TestCase):
    def test_request_thresholds_and_rate_explanation(self):
        standard = price_for("gpt-6.2-sol-medium", DAY, 272_000)
        longer = price_for("gpt-6.2-sol-high", DAY, 272_001)
        self.assertEqual(standard.model, longer.model)
        self.assertEqual(standard.threshold, 272_000)
        self.assertFalse(standard.long_context)
        self.assertTrue(longer.long_context)
        self.assertEqual(standard.rates, (2, .1, 2.5, 10))
        self.assertEqual(longer.rates, (4, .2, 5, 15))
        self.assertEqual(price_for("gemini-3.1-pro", DAY, 200_001).rates, (4, .4, None, 18))

    def test_rate_periods_and_missing_categories(self):
        for model, cutoff, before, after in (
            ("gemini-4-argon", "2026-12-01", (2, .1, None, 10), (4, .2, None, 20)),
            ("gemini-3.8-flash-high", "2027-01-01", (.75, .075, None, 3.75), (1.5, .15, None, 7.5)),
        ):
            with self.subTest(model=model):
                earlier, later = price_for(model, DAY, 100), price_for(model, cutoff, 100)
                self.assertEqual((earlier.rates, earlier.effective_before), (before, cutoff))
                self.assertEqual((later.rates, later.effective_from), (after, cutoff))
                self.assertEqual(earlier.estimate((10, 10, 2, 10))[1], 2)
        missing = price_for("codex-auto-review", DAY, 100)
        self.assertIsNone(missing.model)
        self.assertEqual(missing.estimate((100, 10, 0, 3)), (0, 113))


class ReaderTest(unittest.TestCase):
    def test_active_codex_copy_and_duplicate_events_are_counted_once(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            record = token_count("gpt-5.5", 100, 30, 10)
            active = rollout(root, "duplicated", record, record)
            archive = root / "archived_sessions" / (active.name + ".zst")
            archive.parent.mkdir()
            archive.write_bytes(zstandard.compress(active.read_bytes()))
            self.assertEqual(discover_codex(root), [active])
            self.assertEqual(list(read_codex(active)), list(read_codex(archive)))
            self.assertEqual(len(list(read_codex(active))), 1)

    def test_antigravity_dedup_and_original_orchestrator_label(self):
        with tempfile.TemporaryDirectory() as temp:
            path = antigravity_db(Path(temp))
            before = path.read_bytes()
            (event,) = read_antigravity(path)
            self.assertEqual(event.model, "gemini-4-hydrogen")
            self.assertEqual(event.recorded_model, "gemini-flash-worker-agy")
            self.assertEqual((event.fresh, event.read, event.output, event.reasoning), (100, 1000, 50, 20))
            self.assertEqual(path.read_bytes(), before)


class IndexTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.state, self.codex, self.agy = (root / directory for directory in ("state", "codex", "agy"))

    def open_index(self):
        return UsageIndex(self.state, self.codex, self.agy)

    def test_grouped_rows_and_breakdown_reconcile(self):
        rollout(self.codex, "priced", token_count("gpt-6.2-sol-medium", 3_000, 2_000, 51),
                token_count("gpt-6.2-sol-high", 272_001, 10_000, 37),
                token_count("gpt-5.5", 500, 30, 8, written=22), token_count("codex-auto-review", 30, 0, 1))
        antigravity_db(self.agy)
        index = self.open_index()
        snapshot = index.scan_blocking()
        groups = {row[2]: row for row in snapshot["rows"]}
        self.assertEqual(len(groups["gpt-6.2-sol"][14]), 2)
        self.assertEqual(groups["gpt-5.5"][11], 22)
        self.assertEqual(groups["codex-auto-review"][11], 31)
        plans = snapshot["pricing"]["plans"]
        self.assertEqual(plans[groups["gemini-4-hydrogen"][14][0][0]]["recordedModel"], "gemini-flash-worker-agy")
        for row in snapshot["rows"]:
            expected_counts = [0, 0, 0, 0]
            cost, missing = 0, 0
            for plan_id, *counts in row[14]:
                for i, (count, rate) in enumerate(zip(counts, plans[plan_id]["rates"])):
                    expected_counts[i] += count
                    if rate is None:
                        missing += count
                    else:
                        cost += count * rate / 1e6
            self.assertEqual(row[5:9], expected_counts)
            self.assertAlmostEqual(row[10], cost, places=12)
            self.assertEqual(row[11], missing)
        self.assertEqual(self.open_index().data()[0]["pricing"], snapshot["pricing"])
        with patch("engine.summarize_file", side_effect=AssertionError("unchanged files were read again")):
            self.assertEqual(index.scan_blocking()["rows"], snapshot["rows"])

    def test_upgrade_holds_old_snapshot_until_commit(self):
        source = rollout(self.codex, "upgrade", token_count("gpt-5.5", 100, 0, 10))
        for version in (2, 3):
            with self.subTest(version=version):
                index = self.open_index()
                old_scan = {"scannedAt": "2026-11-20T12:00:00+00:00", "sources": {}}
                old_row = [DAY, "codex", "gpt-5.5-high", "fixture", "codex:upgrade",
                           300, 0, 0, 10, 0, .0018, 0, 1, 0]
                with closing(index.connect()) as db, db:
                    db.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?)",
                               ("codex", str(source), "old", json.dumps([old_row])))
                    db.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)", [
                        ("version", str(version)), ("scan", json.dumps(old_scan)),
                    ])
                index = self.open_index()
                before = index.data()[0]
                self.assertEqual(before["rows"][0][5], 300)
                self.assertIsNone(before["pricing"])

                def fail_after_staging(*_):
                    self.assertEqual(index.data()[0], before)
                    with closing(index.connect()) as db:
                        self.assertEqual(index.version(db), version)
                        self.assertEqual(db.execute("SELECT stamp FROM files").fetchone(), ("old",))
                    raise PermissionError("synthetic file read failure")

                with patch("engine.summarize_file", side_effect=fail_after_staging):
                    with self.assertRaises(IncompleteRebuild):
                        index.scan_blocking()
                self.assertEqual(self.open_index().data()[0], before)
                after = index.scan_blocking()
                self.assertEqual(after["rows"][0][5], 100)
                self.assertIsNotNone(after["pricing"])
                with closing(index.connect()) as db:
                    self.assertEqual(index.version(db), VERSION)
                self.assertEqual(self.open_index().data()[0], after)

    def test_source_states_and_failed_refresh_retain_last_read(self):
        self.codex.mkdir()
        index = self.open_index()
        snapshot = index.scan_blocking()
        self.assertEqual(snapshot["sources"]["agy"]["state"], "missing")
        self.assertEqual(snapshot["sources"]["codex"]["state"], "ok")
        self.assertEqual(snapshot["sources"]["codex"]["files"], 0)
        path = antigravity_db(self.agy)
        rows = index.scan_blocking()["rows"]
        path.write_bytes(b"synthetic corrupt file")
        snapshot = index.scan_blocking()
        self.assertEqual(snapshot["rows"], rows)
        self.assertEqual(snapshot["sources"]["agy"]["errors"], 1)
        self.assertEqual(snapshot["sources"]["agy"]["retained"], 1)
        with patch("engine.discover_antigravity", side_effect=PermissionError):
            snapshot = index.scan_blocking()
        self.assertEqual(snapshot["rows"], rows)
        self.assertEqual(snapshot["sources"]["agy"]["state"], "unreadable")
        self.assertTrue(snapshot["sources"]["agy"]["scannedAt"])


if __name__ == "__main__":
    unittest.main()
