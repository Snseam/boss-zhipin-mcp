"""Candidate database — persistent storage for all searched candidates."""

import json
import os
import logging
from datetime import datetime

log = logging.getLogger("boss-candidate-db")

# Fields to persist from search results
_CANDIDATE_FIELDS = [
    "expectId", "name", "age", "experience", "education", "salary",
    "company", "title", "school", "major", "skills", "jobStatus",
    "expectCity", "fullText", "geekId", "lid", "jid",
]


class CandidateDB:
    """JSON-backed candidate store. Replaces the bare-ID dedup set."""

    def __init__(self, db_path: str, legacy_dedup_path: str | None = None):
        self._path = db_path
        self._data: dict = {"version": 2, "candidates": {}}
        if os.path.exists(db_path):
            self._load()
        elif legacy_dedup_path and os.path.exists(legacy_dedup_path):
            self._migrate_legacy(legacy_dedup_path)
        log.info(f"CandidateDB loaded: {len(self._data['candidates'])} candidates")

    def _load(self):
        with open(self._path, "r") as f:
            self._data = json.load(f)

    def _save(self):
        """Atomic write: write to .tmp then rename."""
        tmp = self._path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self._data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, self._path)

    def _migrate_legacy(self, legacy_path: str):
        """Import bare IDs from seen_candidates.json as legacy entries."""
        try:
            with open(legacy_path, "r") as f:
                old = json.load(f)
            ids = old.get("ids", [])
            now = datetime.now().isoformat(timespec="seconds")
            for eid in ids:
                self._data["candidates"][str(eid)] = {
                    "expectId": str(eid),
                    "status": "legacy",
                    "first_seen": "2026-03-25",
                    "last_updated": now,
                }
            self._save()
            # Rename old file as backup
            bak = legacy_path + ".bak"
            if not os.path.exists(bak):
                os.rename(legacy_path, bak)
            log.info(f"Migrated {len(ids)} IDs from {legacy_path}")
        except Exception as e:
            log.error(f"Migration failed: {e}")

    # --- Core operations ---

    def has(self, expect_id: str) -> bool:
        return str(expect_id) in self._data["candidates"]

    def add(self, candidate: dict, source_keyword: str = "") -> bool:
        """Add a candidate from search results. Returns True if new."""
        eid = str(candidate.get("expectId", ""))
        if not eid or eid in self._data["candidates"]:
            return False
        now = datetime.now().isoformat(timespec="seconds")
        entry = {
            "status": "new",
            "source_keyword": source_keyword,
            "first_seen": datetime.now().strftime("%Y-%m-%d"),
            "last_updated": now,
            "share_url": "",
        }
        for field in _CANDIDATE_FIELDS:
            if field in candidate:
                entry[field] = candidate[field]
        self._data["candidates"][eid] = entry
        return True

    def bulk_add(self, candidates: list[dict], source_keyword: str = "") -> tuple[int, int]:
        """Add multiple candidates. Returns (new_count, dup_count)."""
        new_count = 0
        for c in candidates:
            if self.add(c, source_keyword):
                new_count += 1
        if new_count > 0:
            self._save()
        return new_count, len(candidates) - new_count

    def update(self, expect_id: str, **fields) -> bool:
        """Update specific fields on a candidate. Returns True if found."""
        eid = str(expect_id)
        entry = self._data["candidates"].get(eid)
        if not entry:
            return False
        for k, v in fields.items():
            if v is not None:
                entry[k] = v
        entry["last_updated"] = datetime.now().isoformat(timespec="seconds")
        self._save()
        return True

    def get(self, expect_id: str) -> dict | None:
        return self._data["candidates"].get(str(expect_id))

    def query(
        self,
        status: str | None = None,
        has_share_url: bool | None = None,
        source_keyword: str | None = None,
        date_from: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Query candidates with composable filters."""
        results = []
        for entry in self._data["candidates"].values():
            if status and entry.get("status") != status:
                continue
            if has_share_url is True and not entry.get("share_url"):
                continue
            if has_share_url is False and entry.get("share_url"):
                continue
            if source_keyword and entry.get("source_keyword") != source_keyword:
                continue
            if date_from and entry.get("first_seen", "") < date_from:
                continue
            results.append(entry)
            if len(results) >= limit:
                break
        return results

    def remove_ids(self, expect_ids: list[str]):
        """Remove specific candidates from the database."""
        removed = 0
        for eid in expect_ids:
            if str(eid) in self._data["candidates"]:
                del self._data["candidates"][str(eid)]
                removed += 1
        if removed > 0:
            self._save()
        return removed

    def remove_by_status(self, status: str) -> int:
        """Remove all candidates with a given status."""
        to_remove = [
            eid for eid, c in self._data["candidates"].items()
            if c.get("status") == status
        ]
        return self.remove_ids(to_remove)

    def remove_before_date(self, date_str: str) -> int:
        """Remove all candidates first seen before the given date."""
        to_remove = [
            eid for eid, c in self._data["candidates"].items()
            if c.get("first_seen", "9999") < date_str
        ]
        return self.remove_ids(to_remove)

    def clear_all(self) -> int:
        """Remove all candidates."""
        count = len(self._data["candidates"])
        self._data["candidates"] = {}
        self._save()
        return count

    def stats(self) -> dict:
        """Return summary statistics."""
        candidates = self._data["candidates"]
        by_status: dict[str, int] = {}
        without_share = 0
        for c in candidates.values():
            s = c.get("status", "unknown")
            by_status[s] = by_status.get(s, 0) + 1
            if not c.get("share_url"):
                without_share += 1
        today = datetime.now().strftime("%Y-%m-%d")
        today_new = sum(
            1 for c in candidates.values()
            if c.get("first_seen") == today
        )
        return {
            "total": len(candidates),
            "by_status": by_status,
            "without_share_url": without_share,
            "today_new": today_new,
        }
