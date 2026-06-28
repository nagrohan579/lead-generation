# Lead Finder — Hermes Agent Instructions

You are Hermes. This document tells you everything you need to run the lead finder system.
**All commands go in the `/Lead finder/` directory.**

---

## First-Time Setup (Hetzner Server)

Run this once after uploading the codebase:

```bash
cd "/Lead finder"
bash setup_hetzner.sh
```

That installs Python, Playwright, Chromium, and all dependencies.

---

## How the System Works

- **100 business types** × **90 cities** = **9,000 search combos**
- Each combo scrapes **up to 10 businesses** from Google Maps → up to 90,000 leads total
- Cities are ordered **small → large** so you hit smaller markets first
- All results go to `leads_output.csv` (matches the Lead Tracker format exactly)
- Progress is saved in `progress.json` — if the scraper stops, it resumes from where it left off
- All activity is logged to `scraper.log`

---

## CLI Reference

The entry point is `cli.py`. **All output is JSON.**

### Check overall status
```bash
python3 cli.py status
```
Shows: total combos, how many done, how many remaining, total leads collected.

### See what's available
```bash
python3 cli.py list cities      # all 90 cities with state/timezone
python3 cli.py list types       # all 100 business types
python3 cli.py list done        # combos already scraped
python3 cli.py list pending     # combos not yet run (first 100 shown)
```

### Run the scraper

**Single combo** (one business type in one city):
```bash
python3 cli.py run --type "HVAC Contractors" --city "Providence"
```

**All types for one city** (runs all 100 business types in that city):
```bash
python3 cli.py run --city "Charleston"
```

**All cities in a timezone** (good for batching by calling window):
```bash
python3 cli.py run --timezone EST
python3 cli.py run --timezone CST
python3 cli.py run --timezone MST
python3 cli.py run --timezone PST
```

**Everything remaining** (runs the full 9,000 combo backlog):
```bash
python3 cli.py run --all
```

**Force re-run** a combo already done:
```bash
python3 cli.py run --type "Plumbing Services" --city "Providence" --force
```

### View collected leads
```bash
# Last 50 leads
python3 cli.py view

# Leads for a specific city
python3 cli.py view --city "Providence"

# Leads for a specific combo
python3 cli.py view --city "Providence" --type "HVAC Contractors"

# Only leads that have an email address
python3 cli.py view --has-email

# Only leads that have an owner/contact name
python3 cli.py view --has-owner

# Limit results
python3 cli.py view --limit 20
```

### Export data
```bash
python3 cli.py export --format json   # prints JSON to stdout
python3 cli.py export --format csv    # tells you the CSV file path
```

### Re-run a combo (reset it)
```bash
python3 cli.py reset --type "HVAC Contractors" --city "Providence"
```
Marks the combo as pending. Next `run` will scrape it again.

---

## Watching Live Progress

While a run is going, tail the log:
```bash
tail -f scraper.log
```

Each line looks like:
```
[12:01:18]     [1] Skyview Exteriors — +14013754491 — info@skyviewexteriors.com — Edwin Leonardo
```
Format: `[time] [lead#] Business Name — Phone — Email — Owner Name`

---

## What Data Gets Collected

| Field | Source | Notes |
|-------|--------|-------|
| Business Name | Google Maps | Always present |
| Phone | Google Maps `tel:` link | Always present |
| Website | Google Maps listing | ~70% hit rate |
| Email | Business website scraping | ~35% hit rate |
| Facebook / Instagram / LinkedIn | Business website links | ~40% hit rate |
| Contact Name | Website About page / Yelp / Maps reviews | ~20% hit rate for small local businesses |
| City, State, Country, Timezone, Industry | Known from config | Always present |

If a field can't be found it's left blank — **no fake data**.

---

## Running in Background

To run a batch in the background and come back later:
```bash
nohup python3 cli.py run --city "Charleston" > run_charleston.log 2>&1 &
echo "PID: $!"
```

Check if it's still running:
```bash
ps aux | grep lead_finder
```

---

## Common Patterns

**"Run all small EST cities first"**
```bash
python3 cli.py run --timezone EST
```

**"What do we have for dental clinics?"**
```bash
python3 cli.py view --type "Dental Clinics" --limit 100
```

**"How many leads with emails do we have?"**
```bash
python3 cli.py view --has-email --limit 1 | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['total_matching'], 'leads with email')"
```

**"Run just roofing contractors across all cities"**
No direct flag for this, but you can loop:
```bash
python3 -c "
from config import CITIES
import subprocess
for c in CITIES:
    subprocess.run(['python3','cli.py','run','--type','Roofing Contractors','--city',c[0]])
"
```

---

## Files in This Directory

| File | Purpose |
|------|---------|
| `cli.py` | **Your main entry point** — all commands go through here |
| `lead_finder.py` | Core scraper — called by cli.py, don't run directly unless you want full scan |
| `config.py` | All 100 business types and 90 cities |
| `leads_output.csv` | All collected leads (append-only) |
| `progress.json` | Tracks which combos are done |
| `scraper.log` | Live log of every scrape run |
| `setup_hetzner.sh` | One-shot install script for the server |

---

## If Something Goes Wrong

**Scraper crashes mid-run:**
Progress is saved after every combo. Just run the same command again — it skips already-done combos automatically.

**Getting no results for a combo:**
Google Maps may have returned a CAPTCHA. The combo is still marked done. Reset and retry:
```bash
python3 cli.py reset --type "HVAC Contractors" --city "Providence"
python3 cli.py run --type "HVAC Contractors" --city "Providence"
```

**"City not found" error:**
Check the exact name:
```bash
python3 cli.py list cities | python3 -c "import json,sys; [print(c['city'],c['state']) for c in json.load(sys.stdin)]"
```

**Process seems stuck:**
```bash
ps aux | grep lead_finder   # check if running
tail -20 scraper.log        # see what it last did
```
