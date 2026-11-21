"""Incremental, transactional local index and dashboard data."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from collections import defaultdict
from contextlib import closing
from datetime import datetime
from pathlib import Path

from zstandard import ZstdError

from pricing import PRICE_CHECKED_AT, PRICE_SOURCES, price_for
from readers import (
    UNKNOWN_PROJECT, db_fingerprint, discover_antigravity, discover_codex,
    read_antigravity, read_codex, titles,
)

VERSION = 4
LEGACY_VERSIONS = (2, 3)
MODEL_FAMILY = re.compile(r"^(?:(?:openai|anthropic|google|gemini|vertex_ai)/)?(?:gpt|o\d+|gemini|claude)-", re.I)
MODEL_EFFORT = re.compile(
    r"-(?:extra-(?:low|high)|x-?high|none|minimal|low|medium|high|(?:non-)?thinking)"
    r"(?=$|-\d{4}-\d{2}-\d{2}$|-\d{8}$)", re.I,
)
GEMINI_ROUTING = re.compile(r"^((?:(?:google|gemini|vertex_ai)/)?gemini-(?:4-[a-z]+|3\.8-flash(?:-n)?))-tiered$", re.I)


def display_model(label):
    if not MODEL_FAMILY.match(label):
        return label
    label = MODEL_EFFORT.sub("", label)
    return GEMINI_ROUTING.sub(r"\1", label)


def json_data(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def combine_rows(rows, key_columns=5):
    merged = {}
    for row in rows:
        key = tuple(row[:key_columns])
        if key not in merged:
            merged[key] = list(row)
        else:
            result = merged[key]
            for column in range(key_columns, len(row)):
                result[column] += row[column]
    return list(merged.values())


def summarize_file(source, path):
    reader = read_codex if source == "codex" else read_antigravity
    rows = []
    for event in reader(path):
        day = datetime.fromtimestamp(event.timestamp).date().isoformat()
        project = event.project or UNKNOWN_PROJECT
        project = os.path.normcase(project) if event.project else project
        amounts = (event.fresh, event.read, event.write, event.output)
        price = price_for(event.model, day, sum(amounts[:3]))
        cost, unpriced = price.estimate(amounts)
        rows.append([
            day, source, event.model, project, source + ":" + event.session,
            event.recorded_model or event.model, price,
            *amounts, event.reasoning, cost, unpriced, 1, sum(amounts) if event.approximate_date else 0,
        ])
    return combine_rows(rows, key_columns=7)


def inspect_source(root, discover):
    try:
        if not root.is_dir():
            return [], "missing"
        paths = sorted(discover(root), key=lambda path: path.stat().st_mtime_ns, reverse=True)
    except OSError:
        return [], "unreadable"
    return paths, "ok"


def stable_summary(source, path, fingerprint):
    for _ in range(2):
        rows = summarize_file(source, path)
        current = db_fingerprint(path)
        if current == fingerprint:
            return rows, fingerprint
        fingerprint = current
    raise OSError("source changed during scan")


def local_time():
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


class IncompleteRebuild(Exception):
    pass


class UsageIndex:
    def __init__(self, state_dir, codex_root, agy_root):
        self.path = Path(state_dir) / "usage-index.sqlite3"
        self.codex_root = codex_root
        self.agy_root = agy_root
        self.lock = threading.Lock()
        self.status = {"running": False, "done": 0, "total": 0}
        self.snapshot = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            db.execute("BEGIN")
            self.snapshot = self.load_snapshot(db)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("""
            CREATE TABLE IF NOT EXISTS files (
                source TEXT NOT NULL, path TEXT PRIMARY KEY, stamp TEXT NOT NULL,
                rows TEXT NOT NULL
            )
        """)
        db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        return db

    @staticmethod
    def version(db):
        stored = db.execute("SELECT value FROM meta WHERE key='version'").fetchone()
        return int(stored[0]) if stored else 0

    def load_snapshot(self, db):
        stored = db.execute("SELECT value FROM meta WHERE key='scan'").fetchone()
        version = self.version(db)
        if not stored or version not in (*LEGACY_VERSIONS, VERSION):
            return None
        return self.make_snapshot(db, json.loads(stored[0]), version)

    def make_snapshot(self, db, metadata, version):
        plans = []
        plan_ids = {}
        breakdown = defaultdict(list)

        def display_rows():
            for (raw,) in db.execute("SELECT rows FROM files"):
                for row in json.loads(raw):
                    key = row[:5]
                    key[2] = display_model(key[2])
                    if version != VERSION:
                        yield [*key, *row[5:]]
                        continue
                    recorded, price = row[5:7]
                    signature = json_data([recorded, price])
                    if signature not in plan_ids:
                        plan_ids[signature] = len(plans)
                        model, rates, threshold, long_context, effective_from, effective_before = price
                        plans.append({
                            "recordedModel": recorded, "model": model, "rates": rates,
                            "threshold": threshold, "longContext": long_context,
                            "effectiveFrom": effective_from, "effectiveBefore": effective_before,
                        })
                    breakdown[tuple(key)].append([plan_ids[signature], *row[7:11]])
                    yield [*key, *row[7:]]

        all_rows = combine_rows(display_rows())
        pricing = None
        if version == VERSION:
            for row in all_rows:
                row.append(breakdown[tuple(row[:5])])
            pricing = {"plans": plans, "sources": PRICE_SOURCES, "checkedAt": metadata["pricesCheckedAt"]}
        identified = [Path(path) for (path,) in db.execute("SELECT path FROM files WHERE source='agy'")]
        try:
            names = titles(self.codex_root, identified)
        except OSError:
            names = {}
        indexed_sessions = {row[4] for row in all_rows}
        return {
            **metadata, "rows": all_rows, "pricing": pricing,
            "titles": {key: value for key, value in names.items() if key in indexed_sessions},
            "today": datetime.now().date().isoformat(),
        }

    def start(self):
        with self.lock:
            if self.status["running"]:
                return False
            self.status = {"running": True, "done": 0, "total": 0,
                           "rebuilding": bool(self.snapshot and self.snapshot["pricing"] is None)}
        threading.Thread(target=self._scan, daemon=True, name="local-usage-scan").start()
        return True

    def _scan(self):
        try:
            self.scan_blocking()
        except IncompleteRebuild:
            with self.lock:
                self.status["error"] = "索引重建未完成，已保留旧报表。请检查数据源后重试。"
        except Exception:
            with self.lock:
                self.status["error"] = "刷新未完成。可重试，已完成的快照仍可查看。"
        finally:
            with self.lock:
                self.status["running"] = False

    def scan_blocking(self):
        sources = {
            "codex": inspect_source(self.codex_root, discover_codex),
            "agy": inspect_source(self.agy_root, discover_antigravity),
        }
        with self.lock:
            self.status["total"] = sum(len(paths) for paths, _ in sources.values())
        with closing(self.connect()) as db, db:
            # WAL readers keep seeing the last committed index until this scan commits.
            db.execute("BEGIN IMMEDIATE")
            version = self.version(db)
            if version > VERSION:
                raise ValueError("index was created by a newer application version")
            known = defaultdict(dict)
            for source, path, stamp in db.execute("SELECT source, path, stamp FROM files"):
                known[source][path] = stamp
            rebuilding = version != VERSION
            if rebuilding:
                db.execute("DELETE FROM files")
            done, cached = 0, 0
            diagnostics = {}
            incomplete = False
            for source, (paths, availability) in sources.items():
                previous = known[source]
                discovered = {str(path) for path in paths}
                read, errors, retained = 0, 0, 0
                for path in paths:
                    key = str(path)
                    try:
                        fingerprint = db_fingerprint(path)
                        unchanged = not rebuilding and previous.get(key) == json_data(fingerprint)
                        if not unchanged:
                            rows, fingerprint = stable_summary(source, path, fingerprint)
                    except (OSError, ValueError, TypeError, sqlite3.Error, ZstdError):
                        errors += 1
                        retained += key in previous
                    else:
                        if unchanged:
                            cached += 1
                        else:
                            db.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?)",
                                       (source, key, json_data(fingerprint), json_data(rows)))
                        read += 1
                    done += 1
                    with self.lock:
                        self.status.update(done=done, source=source, cached=cached)
                if availability == "ok":
                    if not rebuilding:
                        db.executemany("DELETE FROM files WHERE path=?",
                                       [(key,) for key in previous.keys() - discovered])
                else:
                    retained = len(previous)
                diagnostics[source] = {"state": availability, "files": len(paths), "read": read,
                                       "errors": errors, "retained": retained, "scannedAt": local_time()}
                incomplete |= bool(errors or availability == "unreadable" or (retained and availability == "missing"))
            with self.lock:
                self.status["sources"] = diagnostics
            if version in LEGACY_VERSIONS and incomplete:
                raise IncompleteRebuild
            metadata = {"scannedAt": local_time(), "sources": diagnostics, "pricesCheckedAt": PRICE_CHECKED_AT}
            db.execute("INSERT OR REPLACE INTO meta VALUES ('version', ?)", (str(VERSION),))
            db.execute("INSERT OR REPLACE INTO meta VALUES('scan',?)", (json_data(metadata),))
            snapshot = self.make_snapshot(db, metadata, VERSION)
        with self.lock:
            self.snapshot = snapshot
        return snapshot

    def data(self):
        with self.lock:
            return self.snapshot, self.status.copy()
