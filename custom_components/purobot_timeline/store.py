"""Disk-backed Purobot image-event archive."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
from zoneinfo import ZoneInfo

from PIL import Image, ImageOps, UnidentifiedImageError

CATEGORIES = ("visits", "toilet_used")
MAX_IMAGE = 12 * 1024 * 1024
MAX_PIXELS = 25_000_000
ID_RE = re.compile(r"^[a-f0-9]{64}$")


class RejectedImage(ValueError):
    """An image update does not meet the archive input contract."""


def parse_timestamp(value, tz_name, now=None):
    if not isinstance(value, str) or len(value) > 64:
        raise RejectedImage("Image entity did not provide a valid update timestamp")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(tz_name))
        epoch = dt.timestamp()
        local = dt.astimezone(ZoneInfo(tz_name))
    except (ValueError, OverflowError, OSError) as err:
        raise RejectedImage("Image entity update timestamp is invalid") from err
    if epoch > (now if now is not None else time.time()) + 300:
        raise RejectedImage("Image update is over five minutes in the future; check the clock")
    return local, epoch


def normalize_image(content, content_type):
    if not isinstance(content, bytes) or not 4 <= len(content) <= MAX_IMAGE:
        raise RejectedImage("Image must be between 4 bytes and 12 MiB")
    if not isinstance(content_type, str) or not content_type.lower().startswith("image/"):
        raise RejectedImage("Purobot entity returned a non-image content type")
    try:
        with Image.open(BytesIO(content)) as im:
            if im.width * im.height > MAX_PIXELS:
                raise RejectedImage("Image exceeds the 25 megapixel limit")
            im.load()
            im = ImageOps.exif_transpose(im)
            rgb = im.convert("RGB")
            full = BytesIO()
            rgb.save(full, format="JPEG", quality=92, optimize=True)
            rgb.thumbnail((400, 250), Image.Resampling.LANCZOS)
            small = BytesIO()
            rgb.save(small, format="JPEG", quality=75, optimize=True)
            return full.getvalue(), small.getvalue()
    except RejectedImage:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as err:
        raise RejectedImage("Purobot entity returned an unreadable image") from err


class EventStore:
    """SQLite index with immutable images; all methods run in HA's executor."""

    def __init__(self, directory, retention_days=30, max_storage_mb=2048):
        self.root = Path(directory).expanduser().absolute()
        self.retention_days = retention_days
        self.max_bytes = max_storage_mb * 1024 * 1024
        self.lock = threading.RLock()

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.root / "timeline.sqlite3", timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def initialize(self):
        with self.lock:
            self.root.mkdir(parents=True, exist_ok=True)
            self.root = self.root.resolve()
            (self.root / "images").mkdir(exist_ok=True)
            (self.root / "thumbs").mkdir(exist_ok=True)
            with self.db() as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS events (
                        id TEXT PRIMARY KEY, epoch REAL NOT NULL, day TEXT NOT NULL,
                        source TEXT NOT NULL, category TEXT NOT NULL,
                        metadata TEXT NOT NULL, bytes INTEGER NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS events_day_time ON events(day, epoch, id);
                    CREATE INDEX IF NOT EXISTS events_epoch ON events(epoch);
                    CREATE INDEX IF NOT EXISTS events_category_day ON events(category, day);
                    CREATE TABLE IF NOT EXISTS diagnostics (key TEXT PRIMARY KEY, value TEXT);
                """)
            with self.db() as db:
                for sub in ("images", "thumbs"):
                    for path in (self.root / sub).iterdir():
                        if (ID_RE.fullmatch(path.stem) and (path.suffix == ".part" or
                                (path.suffix == ".jpg" and not db.execute(
                                    "SELECT 1 FROM events WHERE id=?", (path.stem,)).fetchone()))):
                            path.unlink(missing_ok=True)
            self.cleanup()

    def record_status(self, **values):
        with self.lock, self.db() as db:
            db.executemany("INSERT OR REPLACE INTO diagnostics(key,value) VALUES(?,?)",
                           [(key, json.dumps(value)) for key, value in values.items()])

    def ingest(self, entity_id, category, timestamp, content, content_type, tz_name):
        if category not in CATEGORIES:
            raise RejectedImage("Unknown Purobot event category")
        if not isinstance(entity_id, str) or not entity_id.startswith("image."):
            raise RejectedImage("Source must be a Home Assistant image entity")
        local, epoch = parse_timestamp(timestamp, tz_name)
        full, small = normalize_image(content, content_type)
        content_hash = hashlib.sha256(full).hexdigest()
        ident = hashlib.sha256(json.dumps(
            [entity_id, category, timestamp, content_hash], separators=(",", ":")
        ).encode()).hexdigest()
        meta = {
            "id": ident, "source": entity_id, "category": category,
            "timestamp": local.isoformat(), "epoch": epoch,
            "date": local.date().isoformat(), "time": local.strftime("%H:%M:%S"),
            "memo": "Visit" if category == "visits" else "Toilet Used",
            "categories": [category],
        }
        with self.lock:
            with self.db() as db:
                if db.execute("SELECT 1 FROM events WHERE id=?", (ident,)).fetchone():
                    return "duplicate"
            if epoch < time.time() - self.retention_days * 86400:
                raise RejectedImage("Image update is outside the configured retention period")
            meta["content_hash"] = content_hash
            paths = [self.root / "images" / f"{ident}.jpg", self.root / "thumbs" / f"{ident}.jpg"]
            try:
                for path, blob in zip(paths, (full, small)):
                    temp = path.with_suffix(".part")
                    try:
                        temp.write_bytes(blob)
                        temp.replace(path)
                    finally:
                        temp.unlink(missing_ok=True)
                with self.db() as db:
                    db.execute(
                        "INSERT INTO events(id,epoch,day,source,category,metadata,bytes) VALUES(?,?,?,?,?,?,?)",
                        (ident, epoch, meta["date"], entity_id, category, json.dumps(meta), len(full) + len(small)),
                    )
            except Exception:
                for path in paths:
                    path.unlink(missing_ok=True)
                raise
            self.cleanup()
            self.record_status(last_saved=datetime.now(timezone.utc).isoformat(), last_error=None)
            return "saved"

    def _delete(self, db, ids):
        db.executemany("DELETE FROM events WHERE id=?", [(ident,) for ident in ids])
        db.commit()
        for ident in ids:
            if ID_RE.fullmatch(ident):
                for sub in ("images", "thumbs"):
                    (self.root / sub / f"{ident}.jpg").unlink(missing_ok=True)

    def cleanup(self):
        with self.lock, self.db() as db:
            expired = [r[0] for r in db.execute(
                "SELECT id FROM events WHERE epoch<?", (time.time() - self.retention_days * 86400,)
            )]
            self._delete(db, expired)
            total = db.execute("SELECT COALESCE(SUM(bytes),0) FROM events").fetchone()[0]
            evict = []
            if total > self.max_bytes:
                for row in db.execute("SELECT id,bytes FROM events ORDER BY epoch,id"):
                    evict.append(row["id"])
                    total -= row["bytes"]
                    if total <= self.max_bytes:
                        break
                self._delete(db, evict)
            return len(expired) + len(evict)

    def list_events(self, category, day=None, before=None, limit=80):
        if category not in CATEGORIES:
            raise ValueError("Unknown category")
        if day:
            date.fromisoformat(day)
        if not 1 <= limit <= 200:
            raise ValueError("limit must be 1 to 200")
        with self.lock, self.db() as db:
            dates = [{"date": r[0], "count": r[1]} for r in db.execute(
                "SELECT day,COUNT(*) FROM events GROUP BY day ORDER BY day"
            )]
            latest = dates[-1]["date"] if dates else None
            selected = day or latest
            counts = dict.fromkeys(CATEGORIES, 0)
            if selected:
                counts.update({r[0]: r[1] for r in db.execute(
                    "SELECT category,COUNT(*) FROM events WHERE day=? GROUP BY category", (selected,)
                )})
            where, args = "day=? AND category=?", [selected, category]
            if before:
                if not ID_RE.fullmatch(before):
                    raise ValueError("Invalid cursor")
                pivot = db.execute("SELECT epoch,id FROM events WHERE id=?", (before,)).fetchone()
                if pivot is None:
                    raise ValueError("Page expired; refresh the timeline")
                where += " AND (epoch<? OR (epoch=? AND id<?))"
                args += [pivot["epoch"], pivot["epoch"], pivot["id"]]
            rows = list(db.execute(
                f"SELECT metadata FROM events WHERE {where} ORDER BY epoch DESC,id DESC LIMIT ?",
                args + [limit + 1],
            ))
            has_more = len(rows) > limit
            items = [json.loads(r[0]) for r in reversed(rows[:limit])]
            for item in items:
                item["camera"] = item["source"]
                item["camera_name"] = "Visits" if item["category"] == "visits" else "Toilet Used"
                item["url"] = f'/api/purobot_timeline/image/{item["id"]}'
                item["thumbnail_url"] = f'/api/purobot_timeline/thumbnail/{item["id"]}'
                item["label"] = item["camera_name"]
            total_count, stored_bytes = db.execute(
                "SELECT COUNT(*),COALESCE(SUM(bytes),0) FROM events"
            ).fetchone()
            status = {r[0]: json.loads(r[1]) for r in db.execute("SELECT key,value FROM diagnostics")}
            return {
                "selected_date": selected, "latest_date": latest, "dates": dates,
                "category": category, "counts": counts, "images": items,
                "has_more": has_more, "next_before": items[0]["id"] if has_more and items else None,
                "total_count": total_count, "storage_bytes": stored_bytes,
                "retention_days": self.retention_days,
                "max_storage_mb": self.max_bytes // (1024 * 1024), "status": status,
            }

    def image_path(self, ident, small=False):
        if not ID_RE.fullmatch(ident):
            return None
        with self.lock, self.db() as db:
            if not db.execute("SELECT 1 FROM events WHERE id=?", (ident,)).fetchone():
                return None
            folder = self.root / ("thumbs" if small else "images")
            path = folder / f"{ident}.jpg"
            if path.is_symlink() or folder.is_symlink() or not path.is_file():
                return None
            return path
