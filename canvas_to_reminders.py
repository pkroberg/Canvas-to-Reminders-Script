#!/usr/bin/env python3
"""Sync Canvas assignments (from the calendar .ics feed) into macOS Reminders.

Run via CanvasReminders.app (see run.sh / install.sh) so macOS attributes the
Reminders permission to the app bundle; a bare python binary can't get it.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import icalendar

APP_DIR = Path.home() / "Library/Application Support/CanvasReminders"
STATE_PATH = APP_DIR / "state.json"
LOCK_PATH = APP_DIR / "sync.lock"
LOG_PATH = Path.home() / "Library/Logs/canvas-reminders.log"
DEFAULT_CONFIG = Path(__file__).resolve().parent / "config.toml"

MARKER_RE = re.compile(r"canvas-assignment-id: (\d+)")
# Lines older versions wrote into notes; stripped on sync so notes stay clean.
GENERATED_LINE_RE = re.compile(
    r"^(https?://\S*instructure\.com/\S*|Course: .*|canvas-assignment-id: \d+)$", re.MULTILINE)
UID_RE = re.compile(r"^event-assignment-(?:override-)?(\d+)$")
URL_ID_RE = re.compile(r"#assignment_(\d+)")
# "2026FA-CS-314-001" -> ("CS", "314")
COURSE_RE = re.compile(r"^\d{4}[A-Z]{2}-([A-Z]+)-(\d+[A-Z]?)")

log = logging.getLogger("canvas-reminders")


# ---------------------------------------------------------------- feed ----

@dataclass
class Assignment:
    id: str
    title: str
    course: str      # "CS 314"
    tag: str         # "SoftwareEngineering"
    due: str         # "2026-10-20" (all-day) or "2026-10-20T23:59:00-06:00"
    all_day: bool
    url: str
    is_override: bool = False

    @property
    def due_dt(self) -> datetime:
        if self.all_day:
            return datetime.combine(date.fromisoformat(self.due), datetime.max.time()).astimezone()
        return datetime.fromisoformat(self.due)


def fetch_feed(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "canvas-reminders/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def course_key(raw: str) -> tuple[str, str]:
    """'2026FA-CS-314-001' -> ('CS314', 'CS 314'). Falls back to the raw code."""
    m = COURSE_RE.match(raw)
    if not m:
        key = re.sub(r"[^A-Za-z0-9]", "", raw)
        return key, raw
    return m.group(1) + m.group(2), f"{m.group(1)} {m.group(2)}"


def parse_assignments(ics: bytes, course_tags: dict[str, str]) -> list[Assignment]:
    cal = icalendar.Calendar.from_ical(ics)
    found: dict[str, Assignment] = {}
    for ev in cal.walk("VEVENT"):
        uid = str(ev.get("UID", ""))
        m = UID_RE.match(uid)
        if not m:
            continue  # plain calendar events, holidays, etc.
        url = str(ev.get("URL", ""))
        # Override UIDs carry the override id; the real assignment id is in the URL.
        url_m = URL_ID_RE.search(url)
        aid = url_m.group(1) if url_m else m.group(1)
        is_override = "override" in uid

        summary = str(ev.get("SUMMARY", "")).strip()
        cm = re.match(r"^(.*?)\s*\[([^\]]+)\]\s*$", summary)
        title, raw_course = (cm.group(1), cm.group(2)) if cm else (summary, "")
        title = re.sub(r"\s*\(\d{4}[A-Z]{2}-[^)]*\)\s*$", "", title)  # section suffix on overrides
        key, course = course_key(raw_course) if raw_course else ("Canvas", "Canvas")
        tag = course_tags.get(key, key)

        start = ev.decoded("DTSTART")
        if isinstance(start, datetime):
            due, all_day = start.astimezone().isoformat(timespec="minutes"), False
        else:
            due, all_day = start.isoformat(), True

        a = Assignment(aid, title, course, tag, due, all_day, url, is_override)
        if aid in found and found[aid].is_override and not is_override:
            continue  # section override wins over the base assignment
        found[aid] = a
    return sorted(found.values(), key=lambda a: a.due_dt)


def handoff_notes(a: Assignment) -> str:
    """Temporary marker so we can find the reminder the Shortcut just made; erased right after."""
    return f"canvas-assignment-id: {a.id}"


def strip_generated(text: str) -> str:
    text = GENERATED_LINE_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def due_key(due: str) -> str:
    """'2026-10-20' or '2026-10-20T23:59-06:00' -> local 'YYYY-MM-DD[THH:MM]'."""
    if "T" not in due:
        return due
    return datetime.fromisoformat(due).astimezone().strftime("%Y-%m-%dT%H:%M")


# ----------------------------------------------------------- reminders ----

class RemindersStore:
    """Thin EventKit wrapper scoped to one Reminders list."""

    def __init__(self, list_name: str, create_list: bool = True):
        import EventKit
        from Foundation import NSDateComponents, NSDate

        self.ek, self.NSDateComponents, self.NSDate = EventKit, NSDateComponents, NSDate
        self.store = EventKit.EKEventStore.alloc().init()
        if not self._wait(self.store.requestFullAccessToRemindersWithCompletion_):
            raise PermissionError(
                "Reminders access denied. Enable CanvasReminders in System Settings → "
                "Privacy & Security → Reminders.")
        self.list_created = False
        self.calendar = self._find_or_create_list(list_name, create_list)

    @staticmethod
    def _wait(fn, *args):
        done, box = threading.Event(), {}

        def cb(result, *rest):
            box["r"] = result
            done.set()

        fn(*args, cb)
        if not done.wait(60):
            raise TimeoutError("EventKit call timed out")
        return box["r"]

    def _find_or_create_list(self, name: str, create: bool):
        for cal in self.store.calendarsForEntityType_(self.ek.EKEntityTypeReminder):
            if cal.title() == name:
                return cal
        if not create:
            return None
        cal = self.ek.EKCalendar.calendarForEntityType_eventStore_(self.ek.EKEntityTypeReminder, self.store)
        cal.setTitle_(name)
        cal.setSource_(self.store.defaultCalendarForNewReminders().source())
        ok, err = self.store.saveCalendar_commit_error_(cal, True, None)
        if not ok:
            raise RuntimeError(f"Could not create list {name!r}: {err}")
        log.info("Created Reminders list %r", name)
        self.list_created = True
        return cal

    def all_reminders(self) -> list:
        if self.calendar is None:
            return []
        pred = self.store.predicateForRemindersInCalendars_([self.calendar])
        return list(self._wait(self.store.fetchRemindersMatchingPredicate_completion_, pred) or [])

    def by_marker(self) -> dict[str, object]:
        out = {}
        for r in self.all_reminders():
            m = MARKER_RE.search(r.notes() or "")
            if m:
                out[m.group(1)] = r
        return out

    def unclaimed_index(self, claimed_ids: set[str]) -> dict[tuple[str, str], list]:
        """Reminders not referenced by state, keyed by (title, due key), for lost-state recovery."""
        out: dict[tuple[str, str], list] = {}
        for r in self.all_reminders():
            if r.calendarItemIdentifier() in claimed_ids:
                continue
            c = r.dueDateComponents()
            if c is None:
                continue
            key = f"{c.year():04d}-{c.month():02d}-{c.day():02d}"
            if 0 <= c.hour() <= 23:
                key += f"T{c.hour():02d}:{c.minute():02d}"
            out.setdefault((r.title(), key), []).append(r)
        return out

    @staticmethod
    def _canvas_url(r) -> bool:
        return r.URL() is not None and "instructure.com" in str(r.URL().absoluteString())

    def needs_clean(self, r) -> bool:
        notes = r.notes() or ""
        return strip_generated(notes) != notes or self._canvas_url(r)

    def clean(self, r) -> None:
        """Remove generated notes lines (link, course, ID) and the Canvas URL; keep everything else."""
        r.setNotes_(strip_generated(r.notes() or "") or None)
        if self._canvas_url(r):
            r.setURL_(None)

    def get(self, reminder_id: str | None):
        if not reminder_id or self.calendar is None:
            return None
        r = self.store.calendarItemWithIdentifier_(reminder_id)
        if r is None or r.calendar() is None or r.calendar().calendarIdentifier() != self.calendar.calendarIdentifier():
            return None
        return r

    def _components(self, a: Assignment):
        c = self.NSDateComponents.alloc().init()
        d = a.due_dt
        c.setYear_(d.year)
        c.setMonth_(d.month)
        c.setDay_(d.day)
        if not a.all_day:
            c.setHour_(d.hour)
            c.setMinute_(d.minute)
        return c

    def apply(self, r, a: Assignment, alert_minutes: int) -> None:
        """Set title, due date and alarm. Leaves completion state alone."""
        r.setTitle_(a.title)
        r.setDueDateComponents_(self._components(a))
        r.setStartDateComponents_(None)
        r.setURL_(None)
        if a.all_day:
            r.setAlarms_(None)  # Reminders' own all-day notification time applies
        else:
            fire = a.due_dt - timedelta(minutes=alert_minutes)
            alarm = self.ek.EKAlarm.alarmWithAbsoluteDate_(self.NSDate.dateWithTimeIntervalSince1970_(fire.timestamp()))
            r.setAlarms_([alarm])

    def save(self, r) -> None:
        ok, err = self.store.saveReminder_commit_error_(r, True, None)
        if not ok:
            raise RuntimeError(f"Could not save reminder {r.title()!r}: {err}")

    def create(self, a: Assignment, notes: str, alert_minutes: int):
        r = self.ek.EKReminder.reminderWithEventStore_(self.store)
        r.setCalendar_(self.calendar)
        r.setNotes_(notes)
        self.apply(r, a, alert_minutes)
        self.save(r)
        return r

    def wait_for_marker(self, canvas_id: str, tries: int = 15):
        for _ in range(tries):
            self.store.refreshSourcesIfNecessary()
            self.store.reset()
            r = self.by_marker().get(canvas_id)
            if r is not None:
                return r
            time.sleep(1)
        return None


def shortcut_available(name: str) -> bool:
    try:
        out = subprocess.run(["shortcuts", "list"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return name in out.splitlines()


def create_via_shortcut(shortcut: str, list_name: str, a: Assignment, notes: str) -> bool:
    payload = {"title": a.title, "list": list_name, "tag": a.tag, "notes": notes}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(payload, f)
        path = f.name
    try:
        proc = subprocess.run(["shortcuts", "run", shortcut, "--input-path", path],
                              capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            log.warning("Shortcut failed for %s: %s", a.id, proc.stderr.strip())
            return False
        return True
    except subprocess.SubprocessError as e:
        log.warning("Shortcut error for %s: %s", a.id, e)
        return False
    finally:
        os.unlink(path)


# ---------------------------------------------------------------- state ----

def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except FileNotFoundError:
        return {}


def save_state(state: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=APP_DIR, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, STATE_PATH)


# ----------------------------------------------------------------- sync ----

def sync(assignments: list[Assignment], state: dict, store: RemindersStore | None,
         cfg: dict, dry_run: bool) -> dict[str, int]:
    stats = dict(created=0, updated=0, relinked=0, unchanged=0, skipped_past=0, user_deleted=0, removed=0)
    now = datetime.now().astimezone()
    today = now.date().isoformat()
    alert = int(cfg.get("alert_minutes_before", 0))
    import_cutoff = now - timedelta(days=int(cfg.get("import_past_days", 1)))
    list_name = cfg.get("list_name", "Canvas Script")
    shortcut = cfg.get("shortcut_name", "Canvas Add Reminder")
    use_shortcut = bool(cfg.get("use_shortcut", True)) and not dry_run and shortcut_available(shortcut)
    if cfg.get("use_shortcut", True) and not dry_run and not use_shortcut:
        log.warning("Shortcut %r not found; new reminders get their tag in the notes instead.", shortcut)

    markers = store.by_marker() if store else {}
    claimed = {e.get("reminder_id") for e in state.values() if e.get("reminder_id")}
    unclaimed = store.unclaimed_index(claimed) if store else {}

    def match_unclaimed(title: str, due: str):
        candidates = unclaimed.get((title, due_key(due)))
        return candidates.pop(0) if candidates else None

    # Strip the link / course / ID lines older versions wrote into notes.
    if store:
        # Skip unlinked handoff leftovers: their marker is how this run relinks them.
        to_clean = [r for r in store.all_reminders() if store.needs_clean(r)
                    and (r.calendarItemIdentifier() in claimed or not MARKER_RE.search(r.notes() or ""))]
        if to_clean:
            log.info("%s %d reminders (old link/course/ID notes)", "WOULD CLEAN" if dry_run else "CLEANED", len(to_clean))
            if not dry_run:
                for r in to_clean:
                    store.clean(r)
                    store.save(r)
    # If the whole list was deleted and recreated, start fresh instead of treating
    # every reminder as individually deleted.
    if store and store.list_created:
        for entry in state.values():
            if entry.get("status") == "user_deleted":
                entry["status"] = "active"

    seen = set()
    for a in assignments:
        seen.add(a.id)
        entry = state.get(a.id)
        if entry and entry.get("status") == "user_deleted":
            continue

        r = None
        if store:
            r = store.get(entry.get("reminder_id") if entry else None) or markers.get(a.id)
            if r is None and entry and entry.get("reminder_id"):
                # Identifier may have changed (e.g. iCloud re-sync): look for it by last-known title/due.
                r = match_unclaimed(entry.get("title", ""), entry.get("due", ""))
                if r is not None:
                    entry["reminder_id"] = r.calendarItemIdentifier()
            elif r is None and not entry:
                r = match_unclaimed(a.title, a.due)

        if r is None and entry and entry.get("reminder_id") and store:
            log.info("User deleted reminder for %r; not recreating", a.title)
            entry["status"] = "user_deleted"
            stats["user_deleted"] += 1
            continue

        if r is None and not entry:
            if a.due_dt < import_cutoff:
                stats["skipped_past"] += 1
                continue
            log.info("CREATE  %-12s %-45s due %s", f"#{a.tag}", a.title, a.due)
            stats["created"] += 1
            if dry_run:
                continue
            r = None
            if use_shortcut and create_via_shortcut(shortcut, list_name, a, handoff_notes(a)):
                r = store.wait_for_marker(a.id)
                if r is None:
                    # It may still show up; creating another now could duplicate it.
                    # Next run relinks it by marker, or creates it if it never appeared.
                    log.warning("Shortcut ran but reminder for %s not visible yet; retrying next run", a.id)
                    stats["created"] -= 1
                    continue
                store.clean(r)
                store.apply(r, a, alert)
                store.save(r)
            else:
                r = store.create(a, f"#{a.tag}", alert)  # no Shortcut: the notes are the only place for the tag
            state[a.id] = _entry(a, r, today)
            continue

        if r is not None and not entry:
            # State was lost (or a handoff was interrupted): adopt the existing reminder.
            log.info("RELINK  %-12s %s", f"#{a.tag}", a.title)
            stats["relinked"] += 1
            if not dry_run:
                store.clean(r)
                store.apply(r, a, alert)
                store.save(r)
                state[a.id] = _entry(a, r, today)
            continue

        prev_due = entry.get("due") if entry else None
        prev_title = entry.get("title") if entry else None
        if r is not None and (a.due != prev_due or a.title != prev_title):
            log.info("UPDATE  %-12s %-45s due %s -> %s%s", f"#{a.tag}", a.title, prev_due, a.due,
                     " (completed)" if r.isCompleted() else "")
            stats["updated"] += 1
            if not dry_run:
                store.apply(r, a, alert)
                store.save(r)
        else:
            stats["unchanged"] += 1
        if r is not None and not dry_run:
            state[a.id] = _entry(a, r, today)

    grace = timedelta(days=int(cfg.get("removed_grace_days", 7)))
    for aid, entry in state.items():
        if aid in seen or entry.get("status") != "active":
            continue
        stats["removed"] += 1
        due = datetime.fromisoformat(entry["due"]) if "T" in entry["due"] else \
            datetime.combine(date.fromisoformat(entry["due"]), datetime.max.time()).astimezone()
        recent = due >= now - grace
        log.info("REMOVED %s %r%s", aid, entry.get("title"), "" if recent else " (aged out, no note)")
        if dry_run:
            continue
        r = store.get(entry.get("reminder_id")) if store else None
        if recent and r is not None:
            note = f"⚠️ Removed from Canvas calendar on {today}"
            r.setNotes_(((r.notes() or "") + "\n\n" + note).strip())
            store.save(r)
        entry["status"] = "removed"
    return stats


def _entry(a: Assignment, r, today: str) -> dict:
    return {"reminder_id": r.calendarItemIdentifier(), "title": a.title, "course": a.course,
            "due": a.due, "status": "active", "last_seen": today}


# ----------------------------------------------------------------- main ----

def setup_logging(verbose: bool) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(LOG_PATH)
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(sh)
    log.setLevel(logging.DEBUG if verbose else logging.INFO)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--dry-run", action="store_true", help="show what would change; write nothing")
    p.add_argument("--parse-only", action="store_true", help="print parsed assignments and exit")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    setup_logging(args.verbose)

    cfg = tomllib.loads(args.config.read_text())
    try:
        ics = fetch_feed(cfg["feed_url"])
    except (urllib.error.URLError, TimeoutError) as e:
        log.warning("Could not fetch feed (offline?): %s", e)
        return 0
    assignments = parse_assignments(ics, cfg.get("course_tags", {}))
    if not assignments:
        log.error("Feed parsed to 0 assignments; refusing to sync (bad feed URL?)")
        return 1

    if args.parse_only:
        for a in assignments:
            print(f"{a.id:>8}  {a.due:<26} #{a.tag:<20} {a.title}")
        print(f"{len(assignments)} assignments")
        return 0

    APP_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK_PATH, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.info("Another sync is running; exiting")
            return 0
        store = RemindersStore(cfg.get("list_name", "Canvas Script"), create_list=not args.dry_run)
        state = load_state()
        stats = sync(assignments, state, store, cfg, args.dry_run)
        if not args.dry_run:
            save_state(state)
    log.info("%sDone: %s", "[dry run] " if args.dry_run else "",
             ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in stats.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
