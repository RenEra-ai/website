#!/usr/bin/env python3
"""
Sync roadmap.json status/start/due fields from the GitHub Project.

Source of truth: GitHub Project #1 in the RenEra-ai org.
Editorial copy (milestone titles + summaries, sub-item titles) is hand-curated
in roadmap.json and is NEVER overwritten by this script. Only the following
fields are touched:

    milestones[*].status
    milestones[*].start
    milestones[*].due
    milestones[*].items[*].status

Run locally:
    python3 scripts/sync-roadmap.py

In CI:
    GH_TOKEN=$PROJECTS_READ_TOKEN python3 scripts/sync-roadmap.py

Exit codes:
    0 — wrote roadmap.json (whether or not anything changed)
    1 — gh project fetch failed or roadmap.json is missing/malformed
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ROADMAP_PATH = REPO_ROOT / "roadmap.json"

PROJECT_NUMBER = 1
PROJECT_OWNER = "RenEra-ai"

MILESTONE_PARENT_RE = re.compile(r"^M\d+:")


def fetch_project_items() -> list[dict]:
    """Call `gh project item-list` and return the parsed items."""
    cmd = [
        "gh", "project", "item-list", str(PROJECT_NUMBER),
        "--owner", PROJECT_OWNER,
        "--format", "json",
        "--limit", "200",
    ]
    try:
        proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError:
        sys.exit("error: `gh` not on PATH. Install GitHub CLI or set GH_TOKEN in CI.")
    except subprocess.CalledProcessError as e:
        sys.exit(f"error: gh project item-list failed:\n{e.stderr.strip()}")
    data = json.loads(proc.stdout)
    return data.get("items", [])


def index_by_issue(items: list[dict]) -> dict[int, dict]:
    """{issue_number: {status, start, due}} for items linked to repo issues."""
    out: dict[int, dict] = {}
    for it in items:
        content = it.get("content") or {}
        num = content.get("number")
        if not isinstance(num, int):
            continue
        out[num] = {
            "status": it.get("status") or "Todo",
            "start": it.get("start"),
            "due": it.get("due"),
        }
    return out


def milestone_parent_issue_number(milestone: dict) -> int | None:
    """The parent issue for a milestone is the first sub-item with a M<n>: title."""
    for sub in milestone.get("items", []):
        if isinstance(sub.get("title"), str) and MILESTONE_PARENT_RE.match(sub["title"]):
            return sub.get("n")
    if milestone.get("items"):
        return milestone["items"][0].get("n")
    return None


def update_roadmap(roadmap: dict, by_issue: dict[int, dict]) -> tuple[int, list[str]]:
    """Mutate roadmap in place. Returns (n_changes, change_descriptions)."""
    changes: list[str] = []
    for m in roadmap.get("milestones", []):
        mid = m.get("id", "?")

        # Per-item status
        for sub in m.get("items", []):
            n = sub.get("n")
            ref = by_issue.get(n)
            if ref is None:
                continue
            new_status = ref["status"]
            if sub.get("status") != new_status:
                changes.append(f"{mid} #{n}: {sub.get('status')!r} -> {new_status!r}")
                sub["status"] = new_status

        # Milestone-level status from the parent issue
        parent_n = milestone_parent_issue_number(m)
        parent_ref = by_issue.get(parent_n) if parent_n is not None else None
        if parent_ref is not None and parent_ref["status"] and m.get("status") != parent_ref["status"]:
            changes.append(f"{mid} status: {m.get('status')!r} -> {parent_ref['status']!r}")
            m["status"] = parent_ref["status"]

        # Milestone-level dates: start = min(items.start), due = max(items.due | parent.due)
        starts = [by_issue[s["n"]]["start"] for s in m.get("items", []) if s.get("n") in by_issue and by_issue[s["n"]].get("start")]
        dues = [by_issue[s["n"]]["due"] for s in m.get("items", []) if s.get("n") in by_issue and by_issue[s["n"]].get("due")]
        if starts:
            new_start = min(starts)
            if m.get("start") != new_start:
                changes.append(f"{mid} start: {m.get('start')!r} -> {new_start!r}")
                m["start"] = new_start
        if dues:
            new_due = max(dues)
            if m.get("due") != new_due:
                changes.append(f"{mid} due: {m.get('due')!r} -> {new_due!r}")
                m["due"] = new_due

    return len(changes), changes


def main() -> int:
    if not ROADMAP_PATH.exists():
        sys.exit(f"error: {ROADMAP_PATH} not found")

    roadmap = json.loads(ROADMAP_PATH.read_text(encoding="utf-8"))
    items = fetch_project_items()
    by_issue = index_by_issue(items)

    if not by_issue:
        sys.exit("error: no items returned from gh project item-list (auth or project access?)")

    n, descriptions = update_roadmap(roadmap, by_issue)

    ROADMAP_PATH.write_text(
        json.dumps(roadmap, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    if n == 0:
        print(f"sync-roadmap: no changes ({len(by_issue)} project items inspected)")
    else:
        print(f"sync-roadmap: {n} change(s)")
        for d in descriptions:
            print(f"  - {d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
