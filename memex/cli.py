#!/usr/bin/env python3
"""
memex CLI - wire memex into Claude Code and browse memories from the terminal.

Usage:
    memex install              # global (~/.claude.json)
    memex install --local      # this project only (.claude.json)
    memex install --no-hook    # skip writing the auto-save stop hook
    memex remove               # remove from config
    memex list [--tag TAG]     # show recent entries for this project
    memex search QUERY         # search entries for this project
    memex export DIR           # export entries as linked markdown files
    memex version              # print version
    memex hook-stop            # (internal) called by the Claude Code stop hook
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import (datetime, timedelta, timezone,)
from pathlib import Path

from memex import __version__

GLOBAL_CONFIG = Path.home() / ".claude.json"
LOCAL_CONFIG = Path.cwd() / ".claude.json"

GLOBAL_SETTINGS = Path.home() / ".claude" / "settings.json"
LOCAL_SETTINGS = Path.cwd() / ".claude" / "settings.json"

STOP_HOOK_COMMAND = f"{sys.executable} -m memex.cli hook-stop"

CLAUDE_MD_SNIPPET = """
## memex: Session Memory

At the **start of every session**, call `mem_load` with a brief hint about
what you're working on. This gives you context from past sessions so you
don't rediscover things you already know.

```
mem_load(hint="<what you're about to work on>", files=["<relevant files>"])
```

During a session, call `mem_save` whenever you:
- Finish a meaningful task
- Make an architectural or design decision
- Discover something surprising or easy to get wrong

```
mem_save(
    task="One-sentence summary of what was done",
    files=["list", "of", "files", "touched"],
    decisions=["Any design decisions made"],
    warnings=["Anything surprising or easy to get wrong"],
    tags=["short", "labels"],
    notes="Any extra freeform context"
)
```

Other tools:
- `mem_search(query)` - find past work on a specific topic
- `mem_list()` - see all stored entries
- `mem_update(id, ...)` - update fields on an existing entry
- `mem_delete(id)` - remove stale entries

Memory is stored locally in ~/.memex/ as SQLite - no LLMs, no network.
""".strip()

# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

_DURATION_RE = re.compile(r"^(\d+)([dwmy])$")


def _parse_date(value: str, *, end_of_day: bool = False) -> str:
    value = value.strip()
    units = {"d": 1, "w": 7, "m": 30, "y": 365}
    m = _DURATION_RE.match(value)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = timedelta(days=n * units[unit])
        return (datetime.now(timezone.utc) - delta).isoformat(timespec="seconds")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(
            f"Cannot parse date {value!r}. "
            "Use a relative offset (e.g. '7d', '2w') or an ISO date (e.g. '2026-06-01')."
        )
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if end_of_day and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        dt = dt.replace(hour=23, minute=59, second=59)
    return dt.isoformat(timespec="seconds")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            print(f"  ! Could not parse {path} - treating as empty")
    return {}


def _save_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n")


def _db_path() -> Path:
    db_dir = Path(os.environ.get("MEMEX_DIR", Path.home() / ".memex"))
    if os.environ.get("MEMEX_GLOBAL", "0") == "1":
        return db_dir / "global.db"
    cwd = os.environ.get("MEMEX_PROJECT", os.getcwd())
    safe = re.sub(r"[^a-zA-Z0-9_\-]", "_", cwd).strip("_")[:120]
    return db_dir / f"{safe}.db"


def _format_row(row: dict) -> str:
    lines = [f"#{row['id']} - {row['timestamp'][:16]}  {row['task']}"]
    for field, label in (("files", "Files"), ("decisions", "Decision"), ("warnings", "Warning")):
        try:
            items = json.loads(row[field])
        except (json.JSONDecodeError, TypeError):
            items = []
        for item in items:
            lines.append(f"  {label:<9}: {item}")
    if row.get("raw"):
        lines.append(f"  Notes    : {row['raw']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Hook helpers
# ---------------------------------------------------------------------------

def _install_stop_hook(settings_path: Path) -> None:
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings = _load_json(settings_path)
    hooks = settings.setdefault("hooks", {})
    stop_hooks = hooks.setdefault("Stop", [])

    for entry in stop_hooks:
        for h in entry.get("hooks", []):
            if h.get("command") == STOP_HOOK_COMMAND:
                print(f"  Stop hook already present in {settings_path} - skipped")
                return

    stop_hooks.append({
        "matcher": "",
        "hooks": [{"type": "command", "command": STOP_HOOK_COMMAND}],
    })
    _save_json(settings_path, settings)
    print(f"  Stop hook written: {settings_path}")


def _remove_stop_hook(settings_path: Path) -> bool:
    if not settings_path.exists():
        return False
    settings = _load_json(settings_path)
    stop_hooks = settings.get("hooks", {}).get("Stop", [])
    original_len = len(stop_hooks)
    settings["hooks"]["Stop"] = [
        entry for entry in stop_hooks
        if not any(h.get("command") == STOP_HOOK_COMMAND for h in entry.get("hooks", []))
    ]
    if len(settings["hooks"]["Stop"]) < original_len:
        _save_json(settings_path, settings)
        print(f"  Removed stop hook from {settings_path}")
        return True
    return False


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def install(local: bool = False, no_hook: bool = False) -> None:
    config_path = LOCAL_CONFIG if local else GLOBAL_CONFIG
    settings_path = LOCAL_SETTINGS if local else GLOBAL_SETTINGS
    scope = "local project" if local else "global"
    print(f"Installing memex ({scope})...")

    config = _load_json(config_path)
    config.setdefault("mcpServers", {})

    config["mcpServers"]["memex"] = {
        "type": "stdio",
        "command": sys.executable,
        "args": ["-m", "memex.server"],
    }

    _save_json(config_path, config)
    print(f"  MCP entry written: {config_path}")

    if no_hook:
        print("  Stop hook skipped (--no-hook)")
    else:
        _install_stop_hook(settings_path)

    memex_md = Path.cwd() / ".claude" / "CLAUDE.local.md"
    memex_md.parent.mkdir(parents=True, exist_ok=True)
    if memex_md.exists():
        print("  .claude/CLAUDE.local.md already exists - skipped")
    else:
        memex_md.write_text(CLAUDE_MD_SNIPPET + "\n")
        print("  Created .claude/CLAUDE.local.md")

    git_exclude = Path.cwd() / ".git" / "info" / "exclude"
    entry = ".claude/CLAUDE.local.md"
    if git_exclude.exists() and entry in git_exclude.read_text().splitlines():
        print("  .git/info/exclude already ignores .claude/CLAUDE.local.md - skipped")
    elif git_exclude.exists():
        with git_exclude.open("a") as f:
            f.write(f"\n{entry}\n")
        print("  Added .claude/CLAUDE.local.md to .git/info/exclude")

    print()
    print("Done! Start a new Claude Code session - memex will be active automatically.")
    print(f"Memory DB location: ~/.memex/")


def remove() -> None:
    print("Removing memex...")
    removed = False
    for config_path in [GLOBAL_CONFIG, LOCAL_CONFIG]:
        config = _load_json(config_path)
        mcp_servers = config.get("mcpServers", {})
        for key in ("memex", "claude-mem"):
            if key in mcp_servers:
                del mcp_servers[key]
                _save_json(config_path, config)
                print(f"  Removed '{key}' from {config_path}")
                removed = True

    for settings_path in [GLOBAL_SETTINGS, LOCAL_SETTINGS]:
        if _remove_stop_hook(settings_path):
            removed = True

    memex_md = Path.cwd() / ".claude" / "CLAUDE.local.md"
    if memex_md.exists():
        memex_md.unlink()
        print("  Removed .claude/CLAUDE.local.md")
        removed = True

    if not removed:
        print("  memex not found in any Claude Code config")

    print()
    print("Note: memory DBs kept at ~/.memex/ - delete manually if you want to wipe them.")


def hook_stop() -> None:
    """Called by the Claude Code Stop hook. Checks the transcript for a recent
    mem_save call; if none is found, prompts Claude to save and exits 2 so
    Claude gets one more turn to do so."""
    import json as _json

    try:
        payload = _json.load(sys.stdin)
        transcript_path = payload.get("transcript_path", "")
    except Exception:
        transcript_path = ""

    if transcript_path and Path(transcript_path).exists():
        try:
            lines = Path(transcript_path).read_text().splitlines()
            # Check the last 20 lines for a mem_save tool call
            for line in lines[-20:]:
                try:
                    entry = _json.loads(line)
                except Exception:
                    continue
                # Tool use entries have type "tool_use" and name "mem_save"
                if entry.get("type") == "tool_use" and entry.get("name") == "mem_save":
                    sys.exit(0)
                # Also check nested content arrays (assistant messages)
                for block in entry.get("content", []):
                    if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "mem_save":
                        sys.exit(0)
        except Exception:
            pass

    print(
        "Session is ending. Please call mem_save() now to record what you worked on "
        "so you have context in your next session.",
        file=sys.stderr,
    )
    sys.exit(0)


def list_entries(
    tag: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 20
) -> None:
    db = _db_path()
    if not db.exists():
        print("No memories saved for this project yet.")
        return

    conditions: list[str] = []
    params: list = []

    if tag:
        conditions.append("tags LIKE ?")
        params.append(f'%"{tag}"%')
    if since:
        since_ts = _parse_date(since)
        conditions.append("timestamp >= ?")
        params.append(since_ts)
    if until:
        until_ts = _parse_date(until, end_of_day=True)
        conditions.append("timestamp <= ?")
        params.append(until_ts)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT * FROM entries {where} ORDER BY id DESC LIMIT ?",
        params,
    ).fetchall()
    conn.close()

    if not rows:
        print(f"No entries{f' with tag {tag!r}' if tag else ''}.")
        return

    print(f"=== memex: {len(rows)} entries ({db.name}) ===\n")
    for row in rows:
        print(_format_row(dict(row)))
        print()


def search_entries(query: str, limit: int = 10) -> None:
    db = _db_path()
    if not db.exists():
        print("No memories saved for this project yet.")
        return

    safe = re.sub(r"[^a-zA-Z0-9 _\-]", " ", query).strip()
    if not safe:
        print("Query is empty after sanitisation.")
        return

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT e.* FROM entries e
           JOIN entries_fts f ON f.rowid = e.id
           WHERE entries_fts MATCH ?
           ORDER BY rank LIMIT ?""",
        (safe, limit),
    ).fetchall()
    conn.close()

    if not rows:
        print(f"No entries matched '{query}'.")
        return

    print(f"=== memex: {len(rows)} results for '{query}' ===\n")
    for row in rows:
        print(_format_row(dict(row)))
        print()


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def _slugify(text: str, max_words: int = 6) -> str:
    words = re.sub(r"[^a-z0-9\s-]", "", text.lower()).split()
    slug = "-".join(words[:max_words]).strip("-")
    return slug or "entry"


def _parse_list(row: dict, field: str) -> list:
    try:
        value = json.loads(row[field])
        return value if isinstance(value, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _entry_to_markdown(row: dict, related: list[str]) -> str:
    tags = _parse_list(row, "tags")
    files = _parse_list(row, "files")
    decisions = _parse_list(row, "decisions")
    warnings = _parse_list(row, "warnings")

    lines = ["---", f"id: {row['id']}", f"date: {row['timestamp']}"]
    if tags:
        lines.append(f"tags: [{', '.join(tags)}]")
    lines += ["---", "", f"# {row['task']}", ""]

    if decisions:
        lines.append("## Decisions")
        lines += [f"- {d}" for d in decisions]
        lines.append("")
    if warnings:
        lines.append("## Warnings")
        lines += [f"- {w}" for w in warnings]
        lines.append("")
    if files:
        lines.append("## Files")
        lines += [f"- `{f}`" for f in files]
        lines.append("")
    if row.get("raw"):
        lines += ["## Notes", row["raw"], ""]
    if related:
        lines.append("## Related")
        lines += [f"- [[{name}]]" for name in related]
        lines.append("")
    if tags:
        lines += [" ".join(f"#{t}" for t in tags), ""]

    return "\n".join(lines).rstrip() + "\n"


def export_entries(dest: str) -> None:
    db = _db_path()
    if not db.exists():
        print("No memories saved for this project yet.")
        return

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM entries ORDER BY id").fetchall()]
    conn.close()

    if not rows:
        print("No entries to export.")
        return

    dest_path = Path(dest)
    dest_path.mkdir(parents=True, exist_ok=True)

    # Stable note name per entry: YYYY-MM-DD-slug, disambiguated by id on collision.
    names: dict[int, str] = {}
    used: set[str] = set()
    for row in rows:
        base = f"{(row['timestamp'] or '')[:10]}-{_slugify(row['task'] or '')}"
        name = base if base not in used else f"{base}-{row['id']}"
        used.add(name)
        names[row["id"]] = name

    for row in rows:
        tags = set(_parse_list(row, "tags"))
        files = set(_parse_list(row, "files"))
        related = [
            names[other["id"]]
            for other in rows
            if other["id"] != row["id"]
            and (tags & set(_parse_list(other, "tags")) or files & set(_parse_list(other, "files")))
        ]
        (dest_path / f"{names[row['id']]}.md").write_text(
            _entry_to_markdown(row, related), encoding="utf-8"
        )

    print(f"Exported {len(rows)} entries to {dest_path}/")


def status() -> None:
    from memex import __version__
    db = _db_path()
    print(f"memex {__version__}")
    print(f"DB: {db}")

    if not db.exists():
        print("No memories saved for this project yet.")
        return

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    count = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    last_row = conn.execute("SELECT timestamp FROM entries ORDER BY id DESC LIMIT 1").fetchone()
    tag_rows = conn.execute("SELECT tags FROM entries WHERE tags != '[]'").fetchall()
    conn.close()

    size_kb = db.stat().st_size // 1024
    last_ts = last_row["timestamp"][:16] if last_row else "n/a"
    print(f"Entries: {count}  |  Size: {size_kb} KB  |  Last saved: {last_ts}")

    tag_counts: dict[str, int] = {}
    for row in tag_rows:
        try:
            for t in json.loads(row["tags"]):
                tag_counts[t] = tag_counts.get(t, 0) + 1
        except (json.JSONDecodeError, TypeError):
            pass
    if tag_counts:
        top = sorted(tag_counts.items(), key=lambda x: -x[1])[:8]
        print("Tags: " + ", ".join(f"{t} ({n})" for t, n in top))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="memex - persistent session memory for Claude Code",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"memex {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="command")

    install_parser = subparsers.add_parser("install", help="Install memex into Claude Code")
    install_parser.add_argument("--local", action="store_true",
                                help="Install for this project only")
    install_parser.add_argument("--no-hook", action="store_true",
                                help="Skip writing the auto-save stop hook")

    subparsers.add_parser("remove", help="Remove memex from Claude Code config")

    list_parser = subparsers.add_parser("list", help="Show recent memory entries")
    list_parser.add_argument("--tag", help="Filter by tag")
    list_parser.add_argument("--limit", type=int, default=20, help="Max entries (default 20)")
    list_parser.add_argument("--since", metavar="DATE",
        help="Only show entries after this date (e.g. '7d', '2026-06-01')",
    )
    list_parser.add_argument("--until", metavar="DATE",
        help="Only show entries before this date (e.g. '2026-06-15')",
    )

    search_parser = subparsers.add_parser("search", help="Search memory entries")
    search_parser.add_argument("query", help="Search query")
    search_parser.add_argument("--limit", type=int, default=10, help="Max results (default 10)")

    export_parser = subparsers.add_parser("export", help="Export entries as linked markdown files")
    export_parser.add_argument("dir", help="Destination directory")

    subparsers.add_parser("status", help="Show entry count, DB size, and top tags")
    subparsers.add_parser("version", help="Print version")
    subparsers.add_parser("hook-stop", help="(internal) Auto-save hook called at session end")

    args = parser.parse_args()

    if args.command == "install":
        install(local=args.local, no_hook=args.no_hook)
    elif args.command == "remove":
        remove()
    elif args.command == "list":
        try:
            list_entries(tag=args.tag, limit=args.limit, since=args.since, until=args.until)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(2)
    elif args.command == "search":
        search_entries(query=args.query, limit=args.limit)
    elif args.command == "export":
        export_entries(args.dir)
    elif args.command == "status":
        status()
    elif args.command == "version":
        print(f"memex {__version__}")
    elif args.command == "hook-stop":
        hook_stop()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
