#!/usr/bin/env python3
"""
Lead Finder CLI — for Hermes agent or manual use.

Commands:
  status                        — overview of progress and counts
  run --type X --city Y         — run one combo
  run --city Y                  — run all business types for a city
  run --timezone EST            — run all combos for a timezone
  run --all                     — run everything remaining
  view --city Y --type X        — show leads for a combo
  view --limit N                — show last N leads
  list cities                   — list all available cities
  list types                    — list all business types
  list done                     — list completed combos
  list pending                  — list not-yet-run combos
  reset --type X --city Y       — mark a combo as not done (re-scrape it)
  export --format csv|json      — export all leads

All output is JSON so agents can parse it easily.
"""

import json
import sys
import csv
import argparse
from pathlib import Path
from datetime import datetime

# ── Paths ──────────────────────────────────────────────────────────────────
WORK_DIR = Path(__file__).parent
PROGRESS_FILE = WORK_DIR / "progress.json"
OUTPUT_CSV = WORK_DIR / "leads_output.csv"

from config import BUSINESS_TYPES, CITIES, CSV_COLUMNS

# ── Helpers ────────────────────────────────────────────────────────────────
def out(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False))

def load_progress():
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE) as f:
            return json.load(f)
    return {"done": [], "total_leads": 0}

def save_progress(p):
    with open(PROGRESS_FILE, "w") as f:
        json.dump(p, f, indent=2)

def combo_key(btype, city):
    return f"{btype}||{city}"

def load_leads():
    if not OUTPUT_CSV.exists():
        return []
    with open(OUTPUT_CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def city_lookup(city_name):
    """Find city tuple by name (case-insensitive)."""
    name = city_name.strip().lower()
    for c in CITIES:
        if c[0].lower() == name:
            return c
    return None

def type_lookup(btype_name):
    """Find business type by name (case-insensitive, partial match ok)."""
    name = btype_name.strip().lower()
    for t in BUSINESS_TYPES:
        if t.lower() == name:
            return t
    # Partial match
    for t in BUSINESS_TYPES:
        if name in t.lower():
            return t
    return None

# ── Commands ───────────────────────────────────────────────────────────────

def cmd_status(args):
    progress = load_progress()
    done = set(progress.get("done", []))
    total = len(BUSINESS_TYPES) * len(CITIES)
    leads = load_leads()

    # Count by city and type from done combos
    cities_done = {}
    for key in done:
        parts = key.split("||")
        if len(parts) == 2:
            city = parts[1]
            cities_done[city] = cities_done.get(city, 0) + 1

    out({
        "total_combos": total,
        "done": len(done),
        "remaining": total - len(done),
        "total_leads": len(leads),
        "cities_with_data": len(cities_done),
        "top_cities_done": sorted(cities_done.items(), key=lambda x: -x[1])[:10],
        "business_types_available": len(BUSINESS_TYPES),
        "cities_available": len(CITIES),
        "output_file": str(OUTPUT_CSV),
    })

def cmd_list(args):
    progress = load_progress()
    done = set(progress.get("done", []))

    if args.what == "cities":
        out([{"city": c[0], "state": c[1], "country": c[2], "timezone": c[3]} for c in CITIES])

    elif args.what == "types":
        out(BUSINESS_TYPES)

    elif args.what == "done":
        result = []
        for key in sorted(done):
            parts = key.split("||")
            if len(parts) == 2:
                result.append({"type": parts[0], "city": parts[1]})
        out({"count": len(result), "combos": result})

    elif args.what == "pending":
        result = []
        for city_tuple in CITIES:
            city = city_tuple[0]
            for btype in BUSINESS_TYPES:
                key = combo_key(btype, city)
                if key not in done:
                    result.append({"type": btype, "city": city, "state": city_tuple[1], "timezone": city_tuple[3]})
        out({"count": len(result), "combos": result[:100], "note": "showing first 100" if len(result) > 100 else ""})

    else:
        out({"error": f"Unknown list target: {args.what}. Use: cities, types, done, pending"})

def cmd_view(args):
    leads = load_leads()

    if args.city:
        leads = [l for l in leads if l.get("City", "").lower() == args.city.lower()]
    if args.type:
        leads = [l for l in leads if args.type.lower() in l.get("Industry", "").lower()]
    if args.has_email:
        leads = [l for l in leads if l.get("Email")]
    if args.has_owner:
        leads = [l for l in leads if l.get("Contact Name")]

    limit = args.limit or 50
    subset = leads[-limit:]

    out({
        "total_matching": len(leads),
        "showing": len(subset),
        "leads": subset,
    })

def cmd_reset(args):
    if not args.type or not args.city:
        out({"error": "Need both --type and --city to reset a combo"})
        return

    btype = type_lookup(args.type)
    city_tuple = city_lookup(args.city)
    if not btype:
        out({"error": f"Business type not found: {args.type}"})
        return
    if not city_tuple:
        out({"error": f"City not found: {args.city}"})
        return

    key = combo_key(btype, city_tuple[0])
    progress = load_progress()
    done = set(progress.get("done", []))

    if key in done:
        done.discard(key)
        progress["done"] = list(done)
        save_progress(progress)
        out({"ok": True, "reset": key, "message": "Combo marked as pending — will re-run next time"})
    else:
        out({"ok": True, "message": "Combo was already pending (not in done list)"})

def cmd_export(args):
    leads = load_leads()
    fmt = (args.format or "json").lower()

    if fmt == "json":
        out({"count": len(leads), "leads": leads})
    elif fmt == "csv":
        # Just print the CSV path — it already exists
        out({"file": str(OUTPUT_CSV), "count": len(leads)})
    else:
        out({"error": f"Unknown format: {fmt}. Use json or csv"})

def cmd_run(args):
    """
    Run the scraper for one combo, a full city, a timezone, or all remaining.
    Delegates to lead_finder.py with a targeted combo list.
    """
    import subprocess

    progress = load_progress()
    done = set(progress.get("done", []))

    # Build list of combos to run
    combos = []  # list of (btype, city, state, country, timezone)

    if args.all:
        for city_tuple in CITIES:
            for btype in BUSINESS_TYPES:
                key = combo_key(btype, city_tuple[0])
                if key not in done:
                    combos.append((btype,) + city_tuple)

    elif args.timezone:
        tz = args.timezone.upper()
        for city_tuple in CITIES:
            if city_tuple[3].upper() == tz:
                for btype in BUSINESS_TYPES:
                    key = combo_key(btype, city_tuple[0])
                    if key not in done:
                        combos.append((btype,) + city_tuple)

    elif args.city and not args.type:
        city_tuple = city_lookup(args.city)
        if not city_tuple:
            out({"error": f"City not found: {args.city}"}); return
        for btype in BUSINESS_TYPES:
            key = combo_key(btype, city_tuple[0])
            if key not in done:
                combos.append((btype,) + city_tuple)

    elif args.city and args.type:
        city_tuple = city_lookup(args.city)
        btype = type_lookup(args.type)
        if not city_tuple:
            out({"error": f"City not found: {args.city}"}); return
        if not btype:
            out({"error": f"Business type not found: {args.type}"}); return
        key = combo_key(btype, city_tuple[0])
        if key in done and not args.force:
            out({"ok": True, "message": f"Already done: {key}. Use --force to re-run."}); return
        combos.append((btype,) + city_tuple)

    else:
        out({"error": "Specify what to run: --all, --timezone EST, --city X, or --city X --type Y"}); return

    if not combos:
        out({"ok": True, "message": "Nothing to run — all specified combos are already done"}); return

    out({
        "starting": True,
        "combos_queued": len(combos),
        "first_few": [{"type": c[0], "city": c[1]} for c in combos[:5]],
        "note": "Scraper running — check scraper.log for live output"
    })
    sys.stdout.flush()

    # Write a temp combo list file and run the scraper targeting just those
    combo_file = WORK_DIR / "_run_queue.json"
    with open(combo_file, "w") as f:
        json.dump([{"type": c[0], "city": c[1], "state": c[2], "country": c[3], "timezone": c[4]} for c in combos], f)

    # Run lead_finder with the queue file
    subprocess.run(
        [sys.executable, str(WORK_DIR / "lead_finder.py"), "--queue", str(combo_file)],
        cwd=str(WORK_DIR)
    )

# ── Main ───────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Lead Finder CLI")
    sub = parser.add_subparsers(dest="command")

    # status
    sub.add_parser("status", help="Show overall progress")

    # list
    p_list = sub.add_parser("list", help="List cities, types, done, or pending combos")
    p_list.add_argument("what", choices=["cities", "types", "done", "pending"])

    # view
    p_view = sub.add_parser("view", help="View collected leads")
    p_view.add_argument("--city", help="Filter by city")
    p_view.add_argument("--type", help="Filter by business type")
    p_view.add_argument("--limit", type=int, default=50, help="Max rows to return")
    p_view.add_argument("--has-email", action="store_true", help="Only leads with email")
    p_view.add_argument("--has-owner", action="store_true", help="Only leads with owner name")

    # reset
    p_reset = sub.add_parser("reset", help="Mark a combo as pending (re-run it)")
    p_reset.add_argument("--type", help="Business type")
    p_reset.add_argument("--city", help="City name")

    # export
    p_export = sub.add_parser("export", help="Export all leads")
    p_export.add_argument("--format", choices=["csv", "json"], default="json")

    # run
    p_run = sub.add_parser("run", help="Run the scraper")
    p_run.add_argument("--type", help="Business type (use with --city for single combo)")
    p_run.add_argument("--city", help="City name")
    p_run.add_argument("--timezone", help="Run all pending combos for a timezone (EST/CST/MST/PST)")
    p_run.add_argument("--all", action="store_true", help="Run all remaining combos")
    p_run.add_argument("--force", action="store_true", help="Re-run even if already done")

    args = parser.parse_args()

    if args.command == "status":       cmd_status(args)
    elif args.command == "list":       cmd_list(args)
    elif args.command == "view":       cmd_view(args)
    elif args.command == "reset":      cmd_reset(args)
    elif args.command == "export":     cmd_export(args)
    elif args.command == "run":        cmd_run(args)
    else:
        parser.print_help()
        out({"available_commands": ["status", "list", "view", "reset", "export", "run"]})

if __name__ == "__main__":
    main()
