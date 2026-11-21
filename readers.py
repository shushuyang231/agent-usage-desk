"""Local usage adapters. Only metering metadata is returned."""

from __future__ import annotations

import io
import json
import os
import re
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

import zstandard


UNKNOWN_MODEL = "未知模型"
UNKNOWN_PROJECT = "未识别项目"
UUID = re.compile(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$", re.I)
SAFE_MODEL = re.compile(r"^[\w .()/:-]{1,100}$")


@dataclass(frozen=True)
class Usage:
    timestamp: float
    model: str
    project: str
    session: str
    fresh: int
    read: int
    write: int
    output: int  # Includes reasoning
    reasoning: int
    approximate_date: bool = False


def timestamp(value):
    if isinstance(value, str):
        try:
            stamp = re.sub(r"(\.\d{6})\d+", r"\1", value).replace("Z", "+00:00")
            return datetime.fromisoformat(stamp).timestamp()
        except (ValueError, OverflowError, OSError):
            pass
    return None


def local_project(value):
    if not isinstance(value, str) or not value or "\0" in value:
        return ""
    if value.startswith("file://"):
        url = urlsplit(value)
        value = unquote(url.path)
        if url.netloc and url.netloc.lower() != "localhost":
            value = f"//{url.netloc}{value}"
        elif re.match(r"^/[A-Za-z]:/", value):
            value = value[1:]
    if not (re.match(r"^[A-Za-z]:[/\\]", value) or value.startswith(("/", "\\\\"))):
        return ""
    return os.path.normpath(value)[:1024]


def clean_model(value):
    return value.strip() if isinstance(value, str) and SAFE_MODEL.fullmatch(value.strip()) else ""


def discover_codex(root: Path):
    files = {}
    for archive_rank, folder in enumerate(("archived_sessions", "sessions")):
        for path in (root / folder).rglob("rollout-*.jsonl*"):
            if not path.name.endswith((".jsonl", ".jsonl.zst")) or not path.is_file():
                continue
            key = path.name.removesuffix(".zst")
            rank = (archive_rank, path.suffix == ".jsonl", path.stat().st_mtime_ns)
            if key not in files or rank > files[key][0]:
                files[key] = (rank, path)
    return [entry[1] for entry in files.values()]


def discover_antigravity(root: Path):
    files = {}
    for folder in ("antigravity", "antigravity2", "antigravity-ide", "antigravity-cli", "antigravity-acp"):
        for path in (root / folder / "conversations").glob("*.db"):
            if not path.is_file():
                continue
            stat = path.stat()
            version = (stat.st_mtime_ns, stat.st_size)
            if path.stem not in files or version > files[path.stem][0]:
                files[path.stem] = (version, path)
    return [entry[1] for entry in files.values()]


@contextmanager
def codex_stream(path):
    with path.open("rb") as raw:
        if path.suffix == ".zst":
            with zstandard.ZstdDecompressor().stream_reader(raw, read_across_frames=True) as stream:
                yield io.BufferedReader(stream, buffer_size=512 * 1024)
        else:
            yield raw


def codex_counts(data):
    data = data or {}
    def number(key):
        value = data.get(key, 0)
        return max(0, value) if type(value) is int else 0
    return (
        number("input_tokens"),
        number("cached_input_tokens") or number("cache_read_input_tokens"),
        number("cache_write_input_tokens"),
        number("output_tokens"),
        number("reasoning_output_tokens"),
    )


def codex_event(stamp, model, project, session, counts):
    inp, cached, written, output, reasoning = counts
    cached = min(inp, cached)
    written = min(inp - cached, written)
    output = max(output, reasoning)
    return Usage(stamp, model, project, session, inp - cached - written,
                 cached, written, output, reasoning)


def read_codex(path):
    model, project = UNKNOWN_MODEL, ""
    basename = path.name.removesuffix(".zst").removesuffix(".jsonl")
    match = UUID.search(basename)
    session = match.group() if match else basename
    seen_totals, previous, fork_start = set(), None, None
    pending, markers, accounted = {}, {}, set()
    last_response = None
    with codex_stream(path) as stream:
        for line in stream:
            if not any(marker in line for marker in (
                b'"token_count"', b'"turn_context"', b'"session_meta"', b'"compacted"', b'"token_usage_record"',
            )):
                continue
            try:
                record = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(record, dict):
                continue
            kind = record.get("type")
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            if kind == "session_meta":
                project = local_project(payload.get("cwd")) or project
                session = str(payload.get("id") or session)[:100]
                if payload.get("forked_from_id"):
                    fork_start = timestamp(payload.get("timestamp")) or timestamp(record.get("timestamp"))
                continue
            if kind == "turn_context":
                model = clean_model(payload.get("model")) or model
                project = local_project(payload.get("cwd")) or project
                continue
            stamp = timestamp(record.get("timestamp"))
            if stamp is None or (fork_start and stamp < fork_start):
                continue
            if kind == "token_usage_record":
                response_id = payload.get("response_id")
                counts = codex_counts(payload.get("usage"))
                if not isinstance(response_id, str) or not response_id or not any(counts) or response_id in accounted:
                    continue
                event = codex_event(stamp, clean_model(payload.get("model")) or model, project, session, counts)
                last_response = response_id
                if response_id in markers:
                    yield event
                    accounted.add(response_id)
                else:
                    pending[response_id] = (event, counts, codex_counts(payload.get("thread_token_usage")))
                continue
            if kind == "compacted":
                response_id = payload.get("compaction_response_id")
                if not isinstance(response_id, str) or not response_id or response_id in accounted:
                    continue
                stored = payload.get("latest_token_usage_record") or {}
                markers[response_id] = (stamp, model, project, session, stored)
                if response_id in pending:
                    yield pending.pop(response_id)[0]
                    accounted.add(response_id)
                continue
            if kind != "event_msg" or payload.get("type") != "token_count":
                continue
            info = payload.get("info")
            if not isinstance(info, dict):
                continue
            cumulative = info.get("total_token_usage")
            total = codex_counts(cumulative)
            last = info.get("last_token_usage")
            if cumulative and (total == previous or (stamp, total) in seen_totals):
                continue
            if not last and not cumulative:
                continue
            if last:
                counts = codex_counts(last)
            elif previous and all(now >= old for now, old in zip(total, previous)):
                counts = tuple(now - old for now, old in zip(total, previous))
            else:
                counts = total
            if cumulative:
                previous = total
                seen_totals.add((stamp, total))
                mirror = pending.get(last_response)
                if mirror and (mirror[1] == counts or mirror[2] == total):
                    accounted.add(last_response)
                    pending.pop(last_response)
            if not any(counts):
                continue
            recorded_model = clean_model(info.get("model") or payload.get("model")) or model
            yield codex_event(stamp, recorded_model, project, session, counts)
    for response_id, (stamp, model, project, session, stored) in markers.items():
        if response_id in accounted or not isinstance(stored, dict) or stored.get("response_id") != response_id:
            continue
        counts = codex_counts(stored.get("usage"))
        if any(counts):
            yield codex_event(stamp, clean_model(stored.get("model")) or model, project, session, counts)


# Decoder and three SQLite queries follow the Antigravity adapter in ccusage
# v20.0.51 (MIT, ryoppippi and contributors). See THIRD_PARTY.txt.

def varint(blob, offset):
    number = 0
    for shift in range(0, 70, 7):
        if offset >= len(blob):
            raise ValueError("truncated varint")
        byte = blob[offset]
        offset += 1
        number |= (byte & 127) << shift
        if byte < 128:
            return number, offset
    raise ValueError("invalid varint")


def fields(blob):
    offset = 0
    result = {}
    while offset < len(blob):
        tag, offset = varint(blob, offset)
        tag, wire = divmod(tag, 8)
        if wire == 0:
            value, offset = varint(blob, offset)
        else:
            if wire == 2:
                size, offset = varint(blob, offset)
            elif wire in (1, 5):
                size = 8 if wire == 1 else 4
            else:
                raise ValueError("unsupported protobuf field")
            if offset + size > len(blob):
                raise ValueError("truncated protobuf field")
            value = blob[offset:offset + size] if wire == 2 else None
            offset += size
        result.setdefault(tag, []).append(value)
    return result


def first(values, key, kind, default):
    return next((item for item in values.get(key, ()) if isinstance(item, kind)), default)


def message(values, key):
    return fields(first(values, key, bytes, b""))


def string(values, key):
    return first(values, key, bytes, b"").decode("utf-8", errors="replace").strip()


def proto_time(values):
    seconds = first(values, 1, int, 0)
    return seconds + first(values, 2, int, 0) / 1e9 if 1_000_000_000 < seconds < 4_200_000_000 else None


MODEL_IDS = {
    246: "gemini-2.5-pro", 312: "gemini-2.5-flash", 313: "gemini-2.5-flash-thinking",
    329: "gemini-2.5-flash-thinking", 330: "gemini-2.5-flash-lite",
    281: "claude-sonnet-4", 282: "claude-sonnet-4", 290: "claude-opus-4", 291: "claude-opus-4",
    333: "claude-sonnet-4-5", 334: "claude-sonnet-4-5", 340: "claude-haiku-4-5",
    341: "claude-haiku-4-5", 342: "gpt-oss-120b", 1402: "claude-opus-5-5",
    1417: "gemini-4-argon", 1418: "gemini-4-argon", 1419: "gemini-4-argon",
    1050: "gemini-3.1-flash-lite", 1026: "claude-opus-4-6", 1035: "claude-sonnet-4-6",
    1036: "gemini-3.1-pro", 1037: "gemini-3.1-pro", 1016: "gemini-3.1-pro",
    1018: "gemini-3-flash-preview", 1084: "gemini-3-flash-preview", 1047: "gemini-3-flash-preview",
    1132: "gemini-3.5-flash-high", 1133: "gemini-3.5-flash-high",
    1187: "gemini-3.5-flash-extra-low", 1020: "gemini-3.5-flash-medium",
}
for start, family in ((1318, "3.8"), (1298, "3.7"), (1071, "3.6")):
    MODEL_IDS.update({start + index: f"gemini-{family}-flash-{effort}"
                      for index, effort in enumerate(("high", "medium", "low"))})


def ag_model(label, identifier=0):
    label = clean_model(label).lower()
    placeholder = re.fullmatch(r"model_placeholder_m(\d+)", label)
    if placeholder:
        identifier = 1000 + int(placeholder[1])
        label = ""
    if label:
        label = re.sub(r"\s*\((extra high|xhigh|high|medium|low)\)$", r"-\1", label)
        label = re.sub(r"\s*\([^)]*\)$", "", label).replace(" ", "-")
        return {
            "gemini-pro-default": "gemini-3.1-pro",
            "gemini-pro-agent": "gemini-3.1-pro",
            "gemini-3-flash-agent": "gemini-3.5-flash-high",
            "model_openai_gpt_oss_120b_medium": "gpt-oss-120b",
        }.get(label, label)
    return MODEL_IDS.get(identifier, f"模型 ID {identifier}" if identifier else UNKNOWN_MODEL)


def model_rank(model):
    return 0 if model == UNKNOWN_MODEL else 1 if model.startswith(("模型 ID ", "model_")) else 2


@dataclass
class Generation:
    counts: tuple[int, ...]
    identity: set[tuple[int, str]]
    model: str
    stamp: float | None


def usage_fields(blob):
    data = fields(blob)
    numbers = tuple(first(data, key, int, 0) for key in (1, 2, 3, 4, 5, 9, 10))
    identities = {(key, string(data, key)) for key in (7, 11, 12)}
    return numbers, {item for item in identities if item[1]}


def generations(data, primary_key, retry_key, model, model_id, stamp, fallback):
    primary_blob = first(data, primary_key, bytes, b"")
    primary = usage_fields(primary_blob) if primary_blob else None
    retries = []
    for raw in data.get(retry_key, []):
        if isinstance(raw, bytes):
            blob = first(fields(raw), 2, bytes, b"")
            if blob:
                retries.append(usage_fields(blob))
    if primary:
        mirror = next((i for i, (counts, keys) in enumerate(retries)
                       if counts == primary[0] and keys <= primary[1]), None)
        if mirror is not None:
            retries.pop(mirror)
    for ordinal, (values, keys) in enumerate(([primary] if primary else []) + retries):
        identifier, inp, output, written, cached, reasoning, visible = values
        if not any(values[1:]):
            continue
        candidate = ag_model("", identifier) if identifier else model
        if identifier == model_id and model_rank(model) > model_rank(candidate):
            candidate = model
        if model_rank(candidate) == 0:
            candidate = model
        visible = max(visible, output - reasoning)
        reasoning = max(reasoning, output - visible)
        fallback_keys = fallback if primary and ordinal == 0 else set()
        yield Generation((inp, cached, written, visible, reasoning), keys or fallback_keys,
                         candidate, stamp)


def indices(values):
    for field in values.get(2, []):
        if isinstance(field, int):
            yield field
        elif isinstance(field, bytes):
            offset = 0
            while offset < len(field):
                number, offset = varint(field, offset)
                yield number


def dedupe_generations(items):
    slots = []
    lookup = {}
    for item in items:
        matches = sorted({lookup[key] for key in item.identity if key in lookup})
        if not matches:
            index = len(slots)
            slots.append(item)
        else:
            index = matches[0]
            group = [slots[index], *(slots[k] for k in matches[1:]), item]
            item = Generation(
                tuple(map(max, zip(*(entry.counts for entry in group)))),
                set().union(*(entry.identity for entry in group)),
                max((entry.model for entry in group), key=model_rank),
                min((entry.stamp for entry in group if entry.stamp is not None), default=None),
            )
            for position in matches[1:]:
                slots[position] = None
            slots[index] = item
        lookup.update((key, index) for key in item.identity)
    return (entry for entry in slots if entry is not None)


def db_fingerprint(path):
    stat = path.stat()
    try:
        wal = path.with_name(path.name + "-wal").stat()
        return (stat.st_size, stat.st_mtime_ns, wal.st_size, wal.st_mtime_ns)
    except FileNotFoundError:
        return (stat.st_size, stat.st_mtime_ns, 0, 0)


def antigravity_metadata(path):
    current = db_fingerprint(path)
    uri = path.as_uri() + "?mode=ro"
    # An inactive, unchanged main database can be read without SQLite creating
    # WAL sidecars. Read an active WAL using SQLite's normal snapshot protocol.
    immutable = not path.with_name(path.name + "-wal").exists()
    if immutable:
        uri += "&immutable=1"
    with closing(sqlite3.connect(uri, uri=True, timeout=2)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        tables = {name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "gen_metadata" not in tables:
            return (), (), b""
        rows = db.execute("SELECT idx, data FROM gen_metadata ORDER BY idx").fetchall()
        steps = db.execute(
            "SELECT idx, metadata FROM steps WHERE metadata IS NOT NULL ORDER BY idx"
        ).fetchall() if "steps" in tables else ()
        trajectory = db.execute(
            "SELECT data FROM trajectory_metadata_blob LIMIT 1"
        ).fetchone() if "trajectory_metadata_blob" in tables else None
    if immutable and (path.with_name(path.name + "-wal").exists() or db_fingerprint(path) != current):
        raise OSError("Antigravity database changed during immutable read")
    return rows, steps, (trajectory or (b"",))[0]


def read_antigravity(path):
    rows, steps, trajectory = antigravity_metadata(path)
    root = fields(trajectory or b"")
    project = local_project(string(message(root, 1), 1)) or local_project(string(root, 7))
    start_stamp = proto_time(message(root, 2))
    file_stamp = path.stat().st_mtime
    prepared = []
    for index, blob in rows:
        row = fields(blob)
        data = message(row, 1)
        identifier = first(data, 3, int, 0)
        model = ag_model(string(data, 19) or string(data, 21), identifier)
        stamp = proto_time(message(message(data, 9), 4))
        prepared.append((index, data, identifier, model, stamp, tuple(indices(row))))
    recent_model = next((row[3] for row in reversed(prepared) if model_rank(row[3])), UNKNOWN_MODEL)
    items = []
    for index, blob in steps:
        data = fields(blob)
        mi = message(data, 24)
        identifier = first(mi, 1, int, 0)
        label = string(mi, 12) or string(mi, 8)
        model = ag_model(label, identifier) if (label or identifier) else recent_model
        stamp = proto_time(message(data, 8)) or proto_time(message(data, 1))
        items.extend(generations(data, 9, 28, model, identifier, stamp, {(0, f"step:{index}")}))
    current_model = UNKNOWN_MODEL
    for index, data, identifier, model, stamp, linked in prepared:
        if model_rank(model):
            current_model = model
        identities = {(0, f"step:{step}") for step in linked} or {(0, f"generation:{index}")}
        items.extend(generations(data, 4, 17, current_model, identifier, stamp, identities))
    for event in dedupe_generations(items):
        inp, cached, written, visible, reasoning = event.counts
        yield Usage(event.stamp or start_stamp or file_stamp, event.model, project, path.stem,
                    inp, cached, written, visible + reasoning, reasoning, not event.stamp)


def titles(codex_root, antigravity_paths):
    result = {}
    index = codex_root / "session_index.jsonl"
    if index.exists():
        with index.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                if isinstance(data, dict) and isinstance(data.get("thread_name"), str) and data.get("id"):
                    result["codex:" + str(data["id"])] = data["thread_name"][:200]
    for root in {path.parent.parent for path in antigravity_paths}:
        for path in (root / "annotations").glob("*.pbtxt"):
            if path.stat().st_size > 64_000:
                continue
            match = re.search(r'(?m)^\s*title:\s*("(?:[^"\\]|\\.)*")', path.read_text(
                encoding="utf-8", errors="replace"))
            if match:
                try:
                    result["agy:" + path.stem] = json.loads(match[1])[:200]
                except ValueError:
                    pass
    return result
