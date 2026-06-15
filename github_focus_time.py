#!/usr/bin/env python3
"""
Report cumulative GitHub tab focus time over the last 7 days from Firefox history.
Groups results by repository path (github.com/org/repo).
"""

import argparse
import datetime
import fnmatch
import glob
import shutil
import sqlite3
import sys
import tempfile
import tomllib
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse


def find_places_db() -> Path:
    patterns = [
        "~/.mozilla/firefox/*.default-release/places.sqlite",
        "~/.mozilla/firefox/*.default/places.sqlite",
        "~/.mozilla/firefox/*/places.sqlite",
        # macOS
        "~/Library/Application Support/Firefox/Profiles/*.default-release/places.sqlite",
        "~/Library/Application Support/Firefox/Profiles/*.default/places.sqlite",
        "~/Library/Application Support/Firefox/Profiles/*/places.sqlite",
    ]
    for pattern in patterns:
        matches = glob.glob(str(Path(pattern).expanduser()))
        if matches:
            # pick the most recently modified one if multiple
            return Path(sorted(matches, key=lambda p: Path(p).stat().st_mtime)[-1])
    raise FileNotFoundError(
        "Could not find Firefox places.sqlite. "
        "Pass the path as an argument: python github_focus_time.py /path/to/places.sqlite"
    )


def find_config() -> Path | None:
    candidates = [
        Path.cwd() / "ghtrack.toml",
        Path("~/.config/ghtrack/config.toml").expanduser(),
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


def load_ignore_patterns(config_path: Path | None) -> list[str]:
    """Read the `ignore` list of URL glob patterns from the config file."""
    if config_path is None:
        return []
    with config_path.open("rb") as f:
        data = tomllib.load(f)
    patterns = data.get("ignore", [])
    if not isinstance(patterns, list):
        raise ValueError(f"'ignore' in {config_path} must be a list of URL patterns")
    return [str(p) for p in patterns]


def is_ignored(url: str, patterns: list[str]) -> bool:
    """True if the URL matches any ignore glob pattern (case-insensitive)."""
    lowered = url.lower()
    return any(fnmatch.fnmatch(lowered, p.lower()) for p in patterns)


def repo_path(url: str) -> str | None:
    """
    Extract a GitHub path from a URL:
    - issue/PR:  github.com/org/repo/issues/123  or  github.com/org/repo/pull/456
    - otherwise: github.com/org/repo
    Returns None if not a github.com URL.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return None

    host = parsed.hostname or ""
    if "github.com" not in host:
        return None

    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 4 and parts[2] in ("issues", "pull"):
        return f"github.com/{parts[0]}/{parts[1]}/{parts[2]}/{parts[3]}"
    elif len(parts) >= 2:
        return f"github.com/{parts[0]}/{parts[1]}"
    elif len(parts) == 1:
        return f"github.com/{parts[0]}"
    else:
        return "github.com"


MAX_SESSION_US = 30 * 60 * 1_000_000  # cap per visit: 30 minutes in microseconds


def has_column(con: sqlite3.Connection, table: str, column: str) -> bool:
    rows = con.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == column for row in rows)


def query_focus_time(
    db_path: Path, ignore_patterns: list[str] | None = None
) -> dict[str, dict[str, int]]:
    """Return {date_str: {issue_path: microseconds}}."""
    ignore_patterns = ignore_patterns or []
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
        tmp_path = tmp.name
    shutil.copy2(db_path, tmp_path)

    try:
        con = sqlite3.connect(f"file:{tmp_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row

        cutoff_us = int(__import__("time").time() - 14 * 86400) * 1_000_000

        if has_column(con, "moz_historyvisits", "visit_duration"):
            rows = con.execute("""
                SELECT p.url, v.visit_date, v.visit_duration
                FROM moz_historyvisits v
                JOIN moz_places p ON p.id = v.place_id
                WHERE p.url LIKE '%github.com%'
                  AND v.visit_date >= ?
                  AND v.visit_duration > 0
                  AND v.visit_type != 9
            """, (cutoff_us,)).fetchall()

            # {date: {path: us}}
            totals: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
            for row in rows:
                if is_ignored(row["url"], ignore_patterns):
                    continue
                path = repo_path(row["url"])
                if path:
                    day = _us_to_date(row["visit_date"])
                    totals[day][path] += row["visit_duration"]

        else:
            # Fallback: gap-to-next-visit, capped at 30 min, across all URLs.
            rows = con.execute("""
                SELECT p.url, v.visit_date
                FROM moz_historyvisits v
                JOIN moz_places p ON p.id = v.place_id
                WHERE v.visit_date >= ?
                  AND v.visit_type != 9
                ORDER BY v.visit_date ASC
            """, (cutoff_us,)).fetchall()

            totals = defaultdict(lambda: defaultdict(int))
            visits = [(row["url"], row["visit_date"]) for row in rows]
            for i, (url, ts) in enumerate(visits):
                if is_ignored(url, ignore_patterns):
                    continue
                path = repo_path(url)
                if not path:
                    continue
                if i + 1 < len(visits):
                    duration_us = min(visits[i + 1][1] - ts, MAX_SESSION_US)
                else:
                    duration_us = 0
                if duration_us > 0:
                    day = _us_to_date(ts)
                    totals[day][path] += duration_us

        con.close()
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    return totals


def _us_to_date(visit_date_us: int) -> str:
    return datetime.datetime.utcfromtimestamp(visit_date_us / 1_000_000).strftime("%Y-%m-%d")


def fmt_duration(microseconds: int) -> str:
    seconds = microseconds // 1_000_000
    h, remainder = divmod(seconds, 3600)
    m, s = divmod(remainder, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    elif m:
        return f"{m}m {s:02d}s"
    else:
        return f"{s}s"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "db_path",
        nargs="?",
        help="Path to Firefox places.sqlite (auto-detected if omitted)",
    )
    parser.add_argument(
        "--trim",
        type=float,
        default=5.0,
        metavar="SECONDS",
        help="Drop entries with less than this many seconds of focus time "
        "(default: 5; use 0 to keep everything)",
    )
    args = parser.parse_args()

    if args.db_path:
        db_path = Path(args.db_path)
    else:
        try:
            db_path = find_places_db()
        except FileNotFoundError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    trim_us = int(args.trim * 1_000_000)

    print(f"Reading: {db_path}\n")

    config_path = find_config()
    ignore_patterns = load_ignore_patterns(config_path)
    if config_path:
        print(f"Config: {config_path} ({len(ignore_patterns)} ignore pattern(s))\n")

    totals = query_focus_time(db_path, ignore_patterns)

    if not totals:
        print("No GitHub visits with focus-time data found in the last 7 days.")
        print("(visit_duration may be 0 if Firefox wasn't closed cleanly after visits)")
        return

    grand_total = 0
    for day in sorted(totals):
        day_entries = sorted(
            (item for item in totals[day].items() if item[1] >= trim_us),
            key=lambda x: x[0],
        )
        if not day_entries:
            continue
        day_total = sum(us for _, us in day_entries)
        grand_total += day_total

        weekday = datetime.datetime.strptime(day, "%Y-%m-%d").strftime("%A")
        print(f"\n## {day} {weekday} — {fmt_duration(day_total)}\n")
        for path, us in day_entries:
            url = f"https://{path}"
            parts = path.split("/")
            if len(parts) >= 5 and parts[3] in ("issues", "pull"):
                org_repo = f"`{parts[1]}/{parts[2]}#{parts[4]}`"
            elif len(parts) >= 3:
                org_repo = f"`{parts[1]}/{parts[2]}`"
            else:
                org_repo = f"`{path}`"
            print(f"- [ ] {org_repo}", end="")
            print(f"  {url} — {fmt_duration(us)}")

    print(f"\n**Total: {fmt_duration(grand_total)}**")


if __name__ == "__main__":
    main()
