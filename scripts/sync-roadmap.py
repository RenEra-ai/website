#!/usr/bin/env python3
"""
Sync roadmap.json from the GitHub Project.

Source of truth: GitHub Project #1 in the RenEra-ai org.

The website's editorial model is:
    - GitHub Project owns structure and raw titles.
    - roadmap.json carries milestone summaries and optional curated title overrides.
    - The frontend renders `title_override || title` everywhere a title is shown.

This script:
    - Updates milestone & item `status`, `start`, `due` from the project.
    - Overwrites the raw `title` field (per milestone and per sub-item) so renames
      flow through. `title_override` (curator-owned) is never touched.
    - Adds new items that appear in the project but not in roadmap.json.
    - Drops sub-items that are no longer in the project.
    - Adds new milestones when the project introduces an unknown `M<n>` bucket.
      The new milestone is appended with an empty summary; a curator should fill it.
    - On the first run against the v1 schema, it migrates pre-existing curated
      milestone titles into `title_override` when they differ from the project's
      raw labels. Sub-item titles are NOT migrated — the plan deliberately lets
      renumberings (e.g. M2.6d -> M2.5b) flow through display.

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

ROADMAP_SCHEMA_VERSION = 2

# Matches the milestone "parent" sub-item title (e.g. "M2: First vertical slice").
MILESTONE_PARENT_RE = re.compile(r"^M\d+:")
# Splits a GH project milestone label like "M2 database_to_api_sync Vertical Slice"
# into ("M2", "database_to_api_sync Vertical Slice").
MILESTONE_LABEL_RE = re.compile(r"^(M\d+)\b\s*(.*)$")

MILESTONE_FIELD_ORDER = ["id", "title", "title_override", "status", "start", "due", "summary", "items"]
ITEM_FIELD_ORDER = ["n", "title", "title_override", "status"]


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


def index_project_items(items: list[dict]) -> tuple[dict[str, dict], dict[int, dict]]:
    """Index project items globally and by their M<n> milestone id.

    Returns ({mid: {"label_tail": str, "items": {issue_n: {...}}}}, {issue_n: {...}}).
    Items without a recognizable M<n> milestone stay in the global issue index,
    but cannot be auto-inserted into a milestone bucket.
    """
    buckets: dict[str, dict] = {}
    by_issue: dict[int, dict] = {}
    for it in items:
        content = it.get("content") or {}
        num = content.get("number")
        if not isinstance(num, int):
            continue
        ref = {
            "n": num,
            "status": it.get("status") or "Todo",
            "title": content.get("title") or "",
            "start": it.get("start"),
            "due": it.get("due"),
        }
        by_issue[num] = ref
        ms_title = (it.get("milestone") or {}).get("title") or ""
        match = MILESTONE_LABEL_RE.match(ms_title)
        if not match:
            continue
        mid = match.group(1)
        label_tail = match.group(2).strip()
        bucket = buckets.setdefault(mid, {"label_tail": label_tail, "items": {}})
        if not bucket["label_tail"] and label_tail:
            bucket["label_tail"] = label_tail
        elif bucket["label_tail"] and label_tail and bucket["label_tail"] != label_tail:
            print(
                f"warning: GH project has conflicting labels for {mid}: "
                f"{bucket['label_tail']!r} vs {label_tail!r} — keeping first",
                file=sys.stderr,
            )
        bucket["items"][num] = ref
    return buckets, by_issue


def milestone_parent_issue_number(milestone: dict) -> int | None:
    """The parent issue for a milestone is the first sub-item with a M<n>: title."""
    for sub in milestone.get("items", []):
        if isinstance(sub.get("title"), str) and MILESTONE_PARENT_RE.match(sub["title"]):
            return sub.get("n")
    if milestone.get("items"):
        return milestone["items"][0].get("n")
    return None


def normalize_milestone(m: dict) -> dict:
    out = {k: m[k] for k in MILESTONE_FIELD_ORDER if k in m}
    extras = {k: v for k, v in m.items() if k not in MILESTONE_FIELD_ORDER}
    out.update(extras)
    items = [normalize_item(it) for it in out.get("items", [])]
    items.sort(key=item_sort_key)
    out["items"] = items
    return out


def normalize_item(it: dict) -> dict:
    out = {k: it[k] for k in ITEM_FIELD_ORDER if k in it}
    extras = {k: v for k, v in it.items() if k not in ITEM_FIELD_ORDER}
    out.update(extras)
    return out


def milestone_sort_key(mid: str) -> tuple[int, str]:
    match = re.match(r"^M(\d+)$", str(mid))
    if match:
        return (int(match.group(1)), str(mid))
    return (sys.maxsize, str(mid))


_ITEM_PARENT_RE = re.compile(r"^M\d+:")
_ITEM_NUMBERED_RE = re.compile(r"^M\d+\.(\d+)([A-Za-z]*)\b")
_ITEM_LETTER_RE = re.compile(r"^M\d+\.([A-Za-z]+)")


def item_sort_key(item: dict) -> tuple[int, int, str, int]:
    """Order items as: parent issue < numbered subs (natural) < lettered (e.g. M2.x)."""
    title = item.get("title") or ""
    if _ITEM_PARENT_RE.match(title):
        return (0, 0, "", 0)
    m = _ITEM_NUMBERED_RE.match(title)
    if m:
        return (1, int(m.group(1)), m.group(2), 0)
    m = _ITEM_LETTER_RE.match(title)
    if m:
        return (2, 0, m.group(1), 0)
    return (3, 0, "", int(item.get("n") or 0))


def sync_item_fields(sub: dict, gh: dict, mid: str, migrating: bool, changes: list[str]) -> None:
    # Sub-item titles are NOT migrated into title_override on first sync — the
    # plan deliberately lets renumberings (e.g. M2.6d -> M2.5b) flow through
    # display. Curators may add title_override manually if they want polish.
    del migrating
    if sub.get("n") != gh["n"]:
        sub["n"] = gh["n"]
    if sub.get("status") != gh["status"]:
        changes.append(f"{mid} #{gh['n']}: status {sub.get('status')!r} -> {gh['status']!r}")
        sub["status"] = gh["status"]
    if sub.get("title") != gh["title"]:
        changes.append(f"{mid} #{gh['n']}: title {sub.get('title')!r} -> {gh['title']!r}")
        sub["title"] = gh["title"]


def sync_milestone_rollup(m: dict, refs: dict[int, dict], mid: str, changes: list[str]) -> None:
    parent_n = milestone_parent_issue_number(m)
    parent_ref = refs.get(parent_n) if parent_n is not None else None
    if parent_ref and parent_ref["status"] and m.get("status") != parent_ref["status"]:
        changes.append(f"{mid} status: {m.get('status')!r} -> {parent_ref['status']!r}")
        m["status"] = parent_ref["status"]

    starts = [
        refs[s["n"]]["start"]
        for s in m["items"]
        if s.get("n") in refs and refs[s["n"]].get("start")
    ]
    dues = [
        refs[s["n"]]["due"]
        for s in m["items"]
        if s.get("n") in refs and refs[s["n"]].get("due")
    ]
    if "start" not in m:
        changes.append(f"{mid} start: <absent> -> None")
        m["start"] = None
    if "due" not in m:
        changes.append(f"{mid} due: <absent> -> None")
        m["due"] = None
    new_start = min(starts) if starts else None
    if m["start"] != new_start:
        changes.append(f"{mid} start: {m['start']!r} -> {new_start!r}")
        m["start"] = new_start
    new_due = max(dues) if dues else None
    if m["due"] != new_due:
        changes.append(f"{mid} due: {m['due']!r} -> {new_due!r}")
        m["due"] = new_due


def warn_on_untagged_issues(
    roadmap: dict,
    buckets: dict[str, dict],
    by_issue: dict[int, dict],
    *,
    include_local: bool = False,
) -> None:
    """Print a stderr warning for each project issue whose milestone label has
    no parseable M<n> prefix. By default, items already tracked locally are
    suppressed (they get a separate "retaining" warning during sync). Pass
    include_local=True to surface every untagged issue — used on the abort
    path so operators see the full list when nothing else will run.
    """
    bucketed = {n for b in buckets.values() for n in b["items"]}
    local: set[int] = set()
    if not include_local:
        local = {
            sub.get("n")
            for m in roadmap.get("milestones", [])
            for sub in m.get("items", [])
            if isinstance(sub.get("n"), int)
        }
    for n in sorted(set(by_issue) - bucketed - local):
        title = by_issue[n].get("title", "")
        print(
            f"warning: project issue #{n} ({title!r}) has no recognizable GH milestone label — add one in the project",
            file=sys.stderr,
        )


def sync_roadmap(
    roadmap: dict,
    buckets: dict[str, dict],
    project_by_issue: dict[int, dict] | None = None,
) -> tuple[int, list[str]]:
    """Mutate roadmap in place. Returns (n_changes, change_descriptions)."""
    changes: list[str] = []
    migrating = int(roadmap.get("schema_version", 1)) < ROADMAP_SCHEMA_VERSION

    milestones = roadmap.setdefault("milestones", [])
    local_by_id: dict[str, dict] = {m["id"]: m for m in milestones}
    local_item_by_issue: dict[int, dict] = {}
    local_item_milestone: dict[int, str] = {}
    for m in milestones:
        mid = m.get("id", "?")
        for sub in m.get("items", []):
            n = sub.get("n")
            if isinstance(n, int) and n not in local_item_by_issue:
                local_item_by_issue[n] = sub
                local_item_milestone[n] = mid
    if project_by_issue is None:
        project_by_issue = {
            n: gh
            for bucket in buckets.values()
            for n, gh in bucket["items"].items()
        }
    project_issue_numbers = set(project_by_issue)
    bucketed_issue_numbers = {
        n
        for bucket in buckets.values()
        for n in bucket["items"]
    }

    # 1. Auto-add new milestones for any GH bucket not present locally.
    for mid in sorted(buckets.keys(), key=milestone_sort_key):
        if mid in local_by_id:
            continue
        new_m = {
            "id": mid,
            "title": buckets[mid]["label_tail"],
            "status": "Todo",
            "start": None,
            "due": None,
            "summary": "",
            "items": [],
        }
        roadmap["milestones"].append(new_m)
        local_by_id[mid] = new_m
        changes.append(f"+ milestone {mid}: '{buckets[mid]['label_tail']}' (needs summary)")
        print(
            f"warning: new milestone {mid} added — please add a summary in roadmap.json",
            file=sys.stderr,
        )

    # 2. Sync items, titles, status, and dates per milestone.
    active_milestones: list[dict] = []
    for mid, m in local_by_id.items():
        bucket = buckets.get(mid)
        if not bucket:
            # Local milestone has no GH bucket. Keep it (the user's sync policy
            # auto-removes items but never milestones) and sync any items that
            # still exist in the project. Items that moved to a different
            # bucket get dropped here and re-added under their new milestone
            # in 2b below.
            print(
                f"warning: milestone {mid} has no matching GH bucket — consider removing it from roadmap.json",
                file=sys.stderr,
            )
            merged_items = []
            for sub in m.get("items", []):
                n = sub.get("n")
                if n in bucketed_issue_numbers:
                    # Item moved to another GH milestone; it'll be re-added there.
                    continue
                gh = project_by_issue.get(n)
                if gh is None:
                    changes.append(f"- {mid} #{n}: removed (not in project)")
                    continue
                sync_item_fields(sub, gh, mid, migrating, changes)
                merged_items.append(sub)
            m["items"] = merged_items
            sync_milestone_rollup(m, project_by_issue, mid, changes)
            active_milestones.append(m)
            continue
        gh_items: dict[int, dict] = bucket["items"]
        merged_items: list[dict] = []
        consumed: set[int] = set()

        # 2a. Update or drop existing locals.
        for sub in m.get("items", []):
            n = sub.get("n")
            gh = gh_items.get(n)
            if gh is None:
                if n not in project_issue_numbers:
                    changes.append(f"- {mid} #{n}: removed (not in project)")
                    continue
                if n in bucketed_issue_numbers:
                    # Item moved to a different recognizable bucket; 2b on the
                    # destination milestone will re-add it.
                    continue
                # Item is still in the project but its GH milestone label is
                # missing/unparseable. Retain under the current local milestone
                # rather than silently dropping it.
                orphan = project_by_issue.get(n)
                if orphan is not None:
                    print(
                        f"warning: {mid} #{n} has no recognizable GH milestone — retaining under {mid}",
                        file=sys.stderr,
                    )
                    sync_item_fields(sub, orphan, mid, migrating, changes)
                    merged_items.append(sub)
                    consumed.add(n)
                continue
            sync_item_fields(sub, gh, mid, migrating, changes)
            merged_items.append(sub)
            consumed.add(n)

        # 2b. Append items that are in the project but not yet in this local milestone.
        for n, gh in gh_items.items():
            if n in consumed:
                continue
            sub = local_item_by_issue.get(n, {"n": n})
            previous_mid = local_item_milestone.get(n)
            if previous_mid and previous_mid != mid:
                changes.append(f"{previous_mid} -> {mid} #{n}: moved")
            else:
                changes.append(f"+ {mid} #{n}: '{gh['title']}'")
            sync_item_fields(sub, gh, mid, migrating, changes)
            merged_items.append(sub)

        m["items"] = merged_items

        # 2c. Milestone title: one-shot migration of curated polish into title_override.
        gh_label = bucket["label_tail"]
        local_title = m.get("title")
        if (
            migrating
            and local_title
            and local_title != gh_label
            and "title_override" not in m
        ):
            m["title_override"] = local_title
            changes.append(f"{mid}: title_override seeded with {local_title!r}")
        if local_title != gh_label:
            changes.append(f"{mid}: title {local_title!r} -> {gh_label!r}")
            m["title"] = gh_label

        # 2d. Milestone status and aggregate dates. Use the global by-issue
        # index so retained bucket-less items (and a bucket-less parent issue)
        # can still drive status and date rollups.
        sync_milestone_rollup(m, project_by_issue, mid, changes)
        active_milestones.append(m)

    # 3. Sort milestones by their numeric M<n>.
    active_milestones.sort(key=lambda x: milestone_sort_key(x["id"]))

    # 4. Bump schema version (so migration is one-shot).
    if migrating:
        roadmap["schema_version"] = ROADMAP_SCHEMA_VERSION
        changes.append(f"schema_version -> {ROADMAP_SCHEMA_VERSION}")

    # 5. Normalize field order for clean diffs.
    roadmap["milestones"] = [normalize_milestone(m) for m in active_milestones]

    return len(changes), changes


def main() -> int:
    if not ROADMAP_PATH.exists():
        sys.exit(f"error: {ROADMAP_PATH} not found")

    roadmap = json.loads(ROADMAP_PATH.read_text(encoding="utf-8"))
    items = fetch_project_items()
    buckets, by_issue = index_project_items(items)

    if not by_issue:
        sys.exit("error: no items returned from gh project item-list (auth or project access?)")
    warn_on_untagged_issues(roadmap, buckets, by_issue)
    if not buckets:
        # Surface every untagged issue (including those already in roadmap.json)
        # so the operator can diagnose the project state before fixing labels.
        warn_on_untagged_issues(roadmap, buckets, by_issue, include_local=True)
        sys.exit("error: no project items with recognizable M<n> milestone labels returned")

    n, descriptions = sync_roadmap(roadmap, buckets, by_issue)

    out = {
        "schema_version": roadmap.get("schema_version", ROADMAP_SCHEMA_VERSION),
        "milestones": roadmap["milestones"],
    }
    for k, v in roadmap.items():
        if k not in out:
            out[k] = v

    ROADMAP_PATH.write_text(
        json.dumps(out, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    total_items = len(by_issue)
    if n == 0:
        print(f"sync-roadmap: no changes ({total_items} project items inspected)")
    else:
        print(f"sync-roadmap: {n} change(s) across {total_items} project items")
        for d in descriptions:
            print(f"  - {d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
