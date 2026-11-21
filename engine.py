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

from pricing import estimate
from readers import (
    UNKNOWN_PROJECT, db_fingerprint, discover_antigravity, discover_codex,
    read_antigravity, read_codex, titles,
)

VERSION = 2
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


def combine_rows(rows):
    merged = {}
    for row in rows:
        key = tuple(row[:5])
        if key not in merged:
            merged[key] = list(row)
        else:
            result = merged[key]
            for column in range(5, len(row)):
                result[column] += row[column]
    return list(merged.values())


def summarize_file(source, path):
    reader = read_codex if source == "codex" else read_antigravity
    rows = []
    for event in reader(path):
        day = datetime.fromtimestamp(event.timestamp).date().isoformat()
        project = event.project or UNKNOWN_PROJECT
        project = os.path.normcase(project) if event.project else project
        cost, unpriced = estimate(event)
        tokens = event.fresh + event.read + event.write + event.output
        rows.append([
            day, source, event.model, project, source + ":" + event.session,
            event.fresh, event.read, event.write, event.output, event.reasoning,
            cost, unpriced, 1, tokens if event.approximate_date else 0,
        ])
    return combine_rows(rows)


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
        stored = db.execute("SELECT value FROM meta WHERE key='version'").fetchone()
        if stored != (str(VERSION),):
            with db:
                db.execute("DELETE FROM files")
                db.execute("DELETE FROM meta")
                db.execute("INSERT INTO meta VALUES ('version', ?)", (str(VERSION),))
        return db

    def load_snapshot(self, db):
        stored = db.execute("SELECT value FROM meta WHERE key='scan'").fetchone()
        if not stored:
            return None
        return self.make_snapshot(db, json.loads(stored[0]))

    def make_snapshot(self, db, metadata):
        # Original index rows retain model labels and per-event pricing.
        def display_rows():
            for (raw,) in db.execute("SELECT rows FROM files"):
                for row in json.loads(raw):
                    row[2] = display_model(row[2])
                    yield row

        all_rows = combine_rows(display_rows())
        identified = [Path(path) for (path,) in db.execute("SELECT path FROM files WHERE source='agy'")]
        try:
            names = titles(self.codex_root, identified)
        except OSError:
            names = {}
        indexed_sessions = {row[4] for row in all_rows}
        return {
            **metadata, "rows": all_rows,
            "titles": {key: value for key, value in names.items() if key in indexed_sessions},
            "today": datetime.now().date().isoformat(),
        }

    def start(self):
        with self.lock:
            if self.status["running"]:
                return False
            self.status = {"running": True, "done": 0, "total": 0}
        threading.Thread(target=self._scan, daemon=True, name="local-usage-scan").start()
        return True

    def _scan(self):
        try:
            self.scan_blocking()
        except Exception:
            with self.lock:
                self.status["error"] = "刷新未完成。可重试，已完成的快照仍可查看。"
        finally:
            with self.lock:
                self.status["running"] = False

    def scan_blocking(self):
        sources = {
            "codex": discover_codex(self.codex_root),
            "agy": discover_antigravity(self.agy_root),
        }
        files = [(source, path) for source, paths in sources.items()
                 for path in sorted(paths, key=lambda x: x.stat().st_mtime_ns, reverse=True)]
        with self.lock:
            self.status["total"] = len(files)
        with closing(self.connect()) as db, db:
            known = dict(db.execute("SELECT path, stamp FROM files"))
            discovered = set()
            failures = defaultdict(int)
            sources_ok = defaultdict(int)
            cached = 0
            for done, (source, path) in enumerate(files, 1):
                key = str(path)
                discovered.add(key)
                try:
                    fingerprint = json_data(db_fingerprint(path))
                    if known.get(key) == fingerprint:
                        cached += 1
                    else:
                        rows = summarize_file(source, path)
                        if db_fingerprint(path) != tuple(json.loads(fingerprint)):
                            raise OSError("source changed during scan")
                        db.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?)",
                                   (source, key, fingerprint, json_data(rows)))
                    sources_ok[source] += 1
                except (OSError, ValueError, TypeError, sqlite3.Error):
                    failures[source] += 1
                except Exception as error:
                    if type(error).__module__.startswith("zstandard"):
                        failures[source] += 1
                    else:
                        raise
                with self.lock:
                    self.status.update(done=done, source=source, cached=cached)
            metadata = {
                "scannedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
                "sources": {
                    name: {"files": len(paths), "read": sources_ok[name], "errors": failures[name]}
                    for name, paths in sources.items()
                },
            }
            db.executemany("DELETE FROM files WHERE path=?", [(key,) for key in known.keys() - discovered])
            db.execute("INSERT OR REPLACE INTO meta VALUES('scan',?)", (json_data(metadata),))
            snapshot = self.make_snapshot(db, metadata)
        with self.lock:
            self.snapshot = snapshot
        return snapshot

    def data(self):
        with self.lock:
            return self.snapshot, self.status.copy()
