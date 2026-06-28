#!/usr/bin/env python3
"""
Lead Finder — Free Google Maps scraper
No paid APIs. Uses Playwright + website scraping.
Run: python3 lead_finder.py
"""

import json
import re
import time
import random
import sys
import os
import csv
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse, urljoin, parse_qs, unquote, quote_plus
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

from config import BUSINESS_TYPES, CITIES, CSV_COLUMNS

# ── Paths ──────────────────────────────────────────────────────────────────
WORK_DIR = Path(__file__).parent
PROGRESS_FILE = WORK_DIR / "progress.json"
OUTPUT_CSV = WORK_DIR / "leads_output.csv"
LOG_FILE = WORK_DIR / "scraper.log"

# ── Settings ───────────────────────────────────────────────────────────────
MAX_RESULTS_PER_COMBO = 10   # how many businesses per (type, city) combo
DELAY_BETWEEN_SEARCHES = (4, 8)   # seconds between Google Maps searches
DELAY_BETWEEN_SITES = (2, 5)      # seconds between website visits
DELAY_BETWEEN_CLICKS = (1.5, 3)   # seconds between listing clicks

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# ── Logging ────────────────────────────────────────────────────────────────
def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

# ── Progress tracking ──────────────────────────────────────────────────────
def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE) as f:
            return json.load(f)
    return {"done": [], "total_leads": 0}

def save_progress(progress: dict):
    with open(PROGRESS_FILE, "w") as f:
        json.dump(progress, f, indent=2)

def combo_key(btype: str, city: str) -> str:
    return f"{btype}||{city}"

# ── CSV output ─────────────────────────────────────────────────────────────
def init_csv():
    if not OUTPUT_CSV.exists():
        with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
            writer.writeheader()

def append_leads(leads: list[dict]):
    if not leads:
        return
    with open(OUTPUT_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        for lead in leads:
            row = {col: lead.get(col, "") for col in CSV_COLUMNS}
            writer.writerow(row)

# ── Website contact scraper ────────────────────────────────────────────────
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# Strict phone: requires parens around area code OR dash/space separator — avoids matching coordinates
PHONE_RE = re.compile(
    r"(?:\+?1[-.\s]?)?"             # optional country code
    r"(?:"
    r"\(\d{3}\)[-.\s]\d{3}[-.\s]\d{4}"   # (xxx) xxx-xxxx
    r"|"
    r"\d{3}[-\s]\d{3}[-.\s]\d{4}"         # xxx-xxx-xxxx or xxx xxx-xxxx
    r")"
)

BAD_EMAIL_PARTS = ["@example", "@sentry", "@test", "noreply", "no-reply",
                   "@2x", ".png", ".jpg", ".gif", "schema", "wix", "wordpress"]

# Titles that signal a decision-maker
OWNER_TITLES = [
    "owner", "co-owner", "founder", "co-founder", "ceo", "chief executive",
    "president", "principal", "director", "managing director", "operator",
    "proprietor", "general manager", "gm", "partner",
]

# Words that appear in business names / titles but NOT in person names
NOT_A_NAME = {
    # Days / months
    "monday","tuesday","wednesday","thursday","friday","saturday","sunday",
    "january","february","march","april","june","july","august",
    "september","october","november","december",
    # Social / tech
    "google","facebook","instagram","linkedin","twitter","yelp","youtube",
    # Geography
    "united","states","america","canada","new","north","south","east","west",
    "city","county","street","avenue","road","drive","lane",
    # Business/industry words that show up in company names
    "heating","cooling","hvac","plumbing","electric","electrical","roofing","painting",
    "cleaning","repair","service","services","solutions","systems","group","company",
    "contractors","contractor","construction","management","associates","partners",
    "enterprises","industries","professional","professionals","pro","plus","premier",
    "prime","elite","best","top","all","our","your","the","and","for","with","done",
    "right","true","real","total","full","complete","general","local","national",
    "home","house","property","commercial","residential","industrial","custom",
    "quality","express","rapid","quick","fast","reliable","trusted","expert",
    "experts","master","masters","superior","supreme","first","united","american",
    "dental","medical","law","legal","auto","car","pet","dog","hair","nail",
    "fitness","gym","studio","clinic","center","centre","office","firm","llc",
    "inc","corp","ltd","co",
}

# Regex: two or three capitalised words (a person's name)
NAME_RE = re.compile(r'\b([A-Z][a-z]{1,15}(?:\s+[A-Z][a-z]{1,15}){1,2})\b')

# Rate-limit Google search: one every N seconds at minimum
_last_google_search = 0.0
GOOGLE_SEARCH_MIN_GAP = 3   # seconds between Yelp searches

def clean_email(e: str) -> str:
    if any(b in e.lower() for b in BAD_EMAIL_PARTS):
        return ""
    return e.lower().strip()

# ── Owner / decision-maker name finder ────────────────────────────────────

def _extract_name_near_title(text: str) -> str:
    """Find 'First Last, Owner' or 'Owner: First Last' patterns in plain text."""
    title_pattern = "|".join(re.escape(t) for t in OWNER_TITLES)

    # Pattern A: Name then title  — "John Smith, Owner"
    for m in re.finditer(
        rf'([A-Z][a-z]{{1,15}}(?:\s+[A-Z][a-z]{{1,15}}){{1,2}})'
        rf'\s*[,\-–]?\s*(?:{title_pattern})',
        text, re.IGNORECASE
    ):
        name = m.group(1).strip()
        if _valid_name(name):
            return name

    # Pattern B: Title then name  — "Owner: John Smith" / "Founded by Jane Doe"
    for m in re.finditer(
        rf'(?:{title_pattern})\s*(?:is|:|by|,)?\s*'
        rf'([A-Z][a-z]{{1,15}}(?:\s+[A-Z][a-z]{{1,15}}){{1,2}})',
        text, re.IGNORECASE
    ):
        name = m.group(1).strip()
        if _valid_name(name):
            return name

    # Pattern C: "I'm John" / "My name is Jane Smith"
    for m in re.finditer(
        r"(?:I'?m|my name is)\s+([A-Z][a-z]{1,15}(?:\s+[A-Z][a-z]{1,15})?)",
        text, re.IGNORECASE
    ):
        name = m.group(1).strip()
        if _valid_name(name):
            return name

    return ""

US_STATE_ABBREVS = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA",
    "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ",
    "NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT",
    "VA","WA","WV","WI","WY","DC","ON","BC","AB","QC","MB","SK",
}

def _valid_name(name: str) -> bool:
    parts = name.split()
    if len(parts) < 2:
        return False
    for p in parts:
        if p.upper() in US_STATE_ABBREVS:   # state abbreviation
            return False
        if len(p) <= 1:                      # single letter
            return False
        if not p[0].isupper():               # must start with capital
            return False
        if p.lower() in NOT_A_NAME:
            return False
    return True

def _parse_jsonld_owner(html: str) -> str:
    """Many business sites include JSON-LD with Person/owner data."""
    for script in re.findall(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.DOTALL):
        try:
            data = json.loads(script)
            # data can be a list or dict
            items = data if isinstance(data, list) else [data]
            for item in items:
                # Top-level person
                if item.get("@type") in ("Person",) and item.get("name"):
                    return item["name"]
                # Nested: owner, founder, employee
                for field in ("owner", "founder", "employee", "contactPoint"):
                    sub = item.get(field)
                    if isinstance(sub, dict) and sub.get("name"):
                        return sub["name"]
                    if isinstance(sub, list):
                        for s in sub:
                            if isinstance(s, dict) and s.get("name"):
                                return s["name"]
                # LocalBusiness sometimes has an "employee" array
        except Exception:
            pass
    return ""

def _find_name_in_headings(soup: BeautifulSoup) -> str:
    """h1–h4 tags that contain a name and a title keyword."""
    for tag in soup.find_all(["h1", "h2", "h3", "h4"]):
        text = tag.get_text(" ", strip=True)
        name = _extract_name_near_title(text)
        if name:
            return name
    return ""

def _find_name_in_meta(soup: BeautifulSoup) -> str:
    """<meta name='author'> sometimes has the owner's name."""
    author = soup.find("meta", attrs={"name": "author"})
    if author:
        content = author.get("content", "").strip()
        parts = content.split()
        if 2 <= len(parts) <= 3 and all(p[0].isupper() for p in parts if p):
            if _valid_name(content):
                return content
    return ""

def _find_name_in_copyright(soup: BeautifulSoup) -> str:
    """Footer copyright lines like '© 2024 Mike Johnson Plumbing'."""
    footer = soup.find("footer") or soup
    for el in footer.find_all(string=re.compile(r'©|copyright', re.I)):
        text = str(el).strip()
        # Strip year and ©
        text = re.sub(r'©?\s*\d{4}\s*', '', text).strip()
        # If what's left looks like a name, use it
        matches = NAME_RE.findall(text)
        for name in matches:
            if _valid_name(name):
                return name
    return ""

def _sub_pages_to_check(soup: BeautifulSoup, base_url: str) -> list[str]:
    """Return about/team/staff page URLs from nav links."""
    keywords = ["about", "team", "staff", "our-story", "meet", "who-we-are", "company"]
    found = []
    seen = set()
    base = urlparse(base_url)
    for a in soup.find_all("a", href=True):
        href = a["href"].lower()
        text = a.get_text(strip=True).lower()
        if any(k in href or k in text for k in keywords):
            full = urljoin(base_url, a["href"])
            parsed = urlparse(full)
            if parsed.netloc == base.netloc and full not in seen:
                seen.add(full)
                found.append(full)
        if len(found) >= 3:
            break
    return found

def find_owner_from_website(url: str, session: requests.Session) -> str:
    """Try to extract an owner/decision-maker name from the business website."""
    if not url or not url.startswith("http"):
        return ""

    try:
        resp = session.get(url, timeout=10, allow_redirects=True)
        if resp.status_code != 200:
            return ""
        html = resp.text
        soup = BeautifulSoup(html, "lxml")

        # 1. JSON-LD structured data (most reliable when present)
        name = _parse_jsonld_owner(html)
        if name:
            return name

        # 2. <meta name="author">
        name = _find_name_in_meta(soup)
        if name:
            return name

        # 3. Headings near title keywords
        name = _find_name_in_headings(soup)
        if name:
            return name

        # 4. Full-page text scan for title patterns
        body_text = soup.get_text(" ", strip=True)
        name = _extract_name_near_title(body_text)
        if name:
            return name

        # 5. Copyright footer
        name = _find_name_in_copyright(soup)
        if name:
            return name

        # 6. Try about/team sub-pages
        for sub_url in _sub_pages_to_check(soup, url):
            if sub_url == url:
                continue
            time.sleep(random.uniform(1.5, 3))
            try:
                r2 = session.get(sub_url, timeout=8)
                if r2.status_code != 200:
                    continue
                soup2 = BeautifulSoup(r2.text, "lxml")

                name = _parse_jsonld_owner(r2.text)
                if name: return name
                name = _find_name_in_headings(soup2)
                if name: return name
                name = _extract_name_near_title(soup2.get_text(" ", strip=True))
                if name: return name
            except Exception:
                pass

    except Exception:
        pass

    return ""

def find_owner_from_yelp(business_name: str, city: str, session: requests.Session) -> str:
    """
    Search Yelp for the business — Yelp often has a 'Meet the Business Owner' box
    or review responses signed with the owner's name.
    Rate-limited to avoid IP bans.
    """
    global _last_google_search
    now = time.time()
    gap = now - _last_google_search
    if gap < GOOGLE_SEARCH_MIN_GAP:
        time.sleep(GOOGLE_SEARCH_MIN_GAP - gap + random.uniform(0.5, 1.5))

    search_url = (
        f"https://www.yelp.com/search"
        f"?find_desc={quote_plus(business_name)}"
        f"&find_loc={quote_plus(city)}"
    )
    try:
        resp = session.get(search_url, timeout=8)
        _last_google_search = time.time()
        if resp.status_code != 200:
            return ""

        soup = BeautifulSoup(resp.text, "lxml")

        # Find first business result link
        biz_link = None
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/biz/" in href:
                biz_link = href.split("?")[0]
                if not biz_link.startswith("http"):
                    biz_link = "https://www.yelp.com" + biz_link
                break

        if not biz_link:
            return ""

        time.sleep(random.uniform(1, 2))
        resp2 = session.get(biz_link, timeout=8)
        if resp2.status_code != 200:
            return ""

        soup2 = BeautifulSoup(resp2.text, "lxml")
        text = soup2.get_text(" ", strip=True)

        # Owner replies to reviews — "- Mike, Owner" pattern
        for m in re.finditer(
            r'[-–]\s*([A-Z][a-z]{1,15}(?:\s+[A-Z][a-z]{1,15})?)\s*,?\s*(?:owner|founder|ceo)',
            text, re.IGNORECASE
        ):
            candidate = m.group(1).strip()
            if _valid_name(candidate):
                return candidate

        # General title-near-name scan on Yelp page
        name = _extract_name_near_title(text)
        if name:
            return name

    except Exception:
        pass

    return ""

def _collect_sub_pages(soup: BeautifulSoup, base_url: str) -> list[str]:
    """Collect contact/about URLs from the homepage for deeper email/social scraping."""
    keywords = ["contact", "about", "team", "staff", "reach"]
    found = []
    seen = set()
    base = urlparse(base_url)
    for a in soup.find_all("a", href=True):
        href_lower = a["href"].lower()
        text_lower = a.get_text(strip=True).lower()
        if any(k in href_lower or k in text_lower for k in keywords):
            full = urljoin(base_url, a["href"])
            parsed = urlparse(full)
            if parsed.netloc == base.netloc and full not in seen:
                seen.add(full)
                found.append(full)
        if len(found) >= 2:
            break
    return found

def scrape_website_full(url: str, business_name: str = "", city: str = "", skip_owner: bool = False) -> dict:
    info = {
        "Email": "", "LinkedIn": "", "Facebook": "",
        "Instagram": "", "Contact Name": "",
    }
    if not url or not url.startswith("http"):
        return info

    session = requests.Session()
    session.headers.update(HEADERS)

    def _parse_page(html: str):
        soup_tmp = BeautifulSoup(html, "lxml")

        if not info["Email"]:
            for a in soup_tmp.find_all("a", href=True):
                if a["href"].startswith("mailto:"):
                    email = clean_email(a["href"].replace("mailto:", "").split("?")[0])
                    if email:
                        info["Email"] = email
                        break
            if not info["Email"]:
                for e in EMAIL_RE.findall(html):
                    e = clean_email(e)
                    if e:
                        info["Email"] = e
                        break

        for a in soup_tmp.find_all("a", href=True):
            href = a["href"]
            if ("linkedin.com/in/" in href or "linkedin.com/company/" in href) and not info["LinkedIn"]:
                info["LinkedIn"] = href.split("?")[0]
            elif "facebook.com/" in href and "facebook.com/tr" not in href and not info["Facebook"]:
                info["Facebook"] = href.split("?")[0]
            elif "instagram.com/" in href and not info["Instagram"]:
                info["Instagram"] = href.split("?")[0]

    try:
        resp = session.get(url, timeout=10, allow_redirects=True)
        if resp.status_code == 200:
            _parse_page(resp.text)
            soup = BeautifulSoup(resp.text, "lxml")

            # Try contact/about sub-pages for email and socials
            for sub_url in _collect_sub_pages(soup, url):
                if all([info["Email"], info["Facebook"], info["Instagram"]]):
                    break
                time.sleep(random.uniform(1, 2))
                try:
                    r2 = session.get(sub_url, timeout=8)
                    if r2.status_code == 200:
                        _parse_page(r2.text)
                except Exception:
                    pass

            # Owner name — website first, Yelp fallback (unless Maps already found one)
            if not skip_owner:
                name = find_owner_from_website(url, session)
                if not name and business_name:
                    name = find_owner_from_yelp(business_name, city, session)
                info["Contact Name"] = name

    except Exception:
        pass

    return info

# ── Google Maps scraper ────────────────────────────────────────────────────
def scrape_maps(page, business_type: str, city: str, state: str, country: str, timezone: str) -> list[dict]:
    """Scrape Google Maps for businesses matching (business_type, city)."""
    query = f"{business_type} in {city} {state}"
    url = f"https://www.google.com/maps/search/{query.replace(' ', '+')}"
    leads = []

    log(f"  Searching: {query}")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        time.sleep(random.uniform(2, 3))

        # If Google redirected to consent.google.com (EU servers), handle it
        # and navigate back. Use jsname attribute — stable across all languages.
        if "consent.google.com" in page.url:
            try:
                # jsname="b3VHJd" is the Accept-all button in every language
                page.click('button[jsname="b3VHJd"]', timeout=5000)
                time.sleep(1.5)
            except Exception:
                try:
                    # Fallback: first submit button in the form is always Accept
                    page.locator('form button[type="submit"]').first.click(timeout=3000)
                    time.sleep(1.5)
                except Exception:
                    pass
            # Navigate back to Maps after dismissing consent
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            time.sleep(random.uniform(2, 3))

        # Wait for results feed
        try:
            page.wait_for_selector('[role="feed"]', timeout=15_000)
        except PlaywrightTimeout:
            log("    No results feed found — skipping")
            return leads

        # Scroll the feed until Maps stops loading new results.
        # Stop when two consecutive scrolls produce no new listings.
        seen_hrefs = []
        stale_scrolls = 0
        MAX_STALE = 3      # stop after this many scrolls with no new links
        MAX_SAFETY = 200   # hard ceiling to avoid infinite loops

        while stale_scrolls < MAX_STALE and len(seen_hrefs) < MAX_SAFETY:
            page.evaluate('() => { const f = document.querySelector(\'[role="feed"]\'); if(f) f.scrollTop += 800; }')
            time.sleep(random.uniform(1.5, 2.5))

            # Check if Maps signalled "end of results"
            end_marker = page.query_selector('span.HlvSq, [class*="end-of-list"], [jslog*="end_of_list"]')
            if end_marker and end_marker.is_visible():
                # Grab whatever is in the feed now, then stop
                pass  # fall through to collect, then break below

            anchors = page.query_selector_all('[role="feed"] a[href*="/maps/place/"]')
            before = len(seen_hrefs)
            for a in anchors:
                href = a.get_attribute("href")
                if href and href not in seen_hrefs:
                    seen_hrefs.append(href)

            if len(seen_hrefs) == before:
                stale_scrolls += 1
            else:
                stale_scrolls = 0  # reset on progress

            if end_marker and end_marker.is_visible():
                break

        log(f"    Found {len(seen_hrefs)} listing links")

        for i, listing_url in enumerate(seen_hrefs):  # no cap — process everything found
            try:
                time.sleep(random.uniform(*DELAY_BETWEEN_CLICKS))
                page.goto(listing_url, wait_until="domcontentloaded", timeout=20_000)
                time.sleep(random.uniform(1.5, 2.5))

                lead = _extract_listing(page)
                if not lead.get("Business Name"):
                    continue

                # Try to get owner name from Maps page (while browser is already here)
                maps_owner = _owner_from_maps_page(page)
                if maps_owner:
                    lead["Contact Name"] = maps_owner

                lead["City"] = city
                lead["State / Province"] = state
                lead["Country"] = country
                lead["Timezone"] = timezone
                lead["Industry"] = business_type
                lead["Status"] = "New"
                lead["Total Calls Made"] = "0"

                # Scrape website for email, socials, owner name (if Maps didn't find one)
                if lead.get("Website"):
                    time.sleep(random.uniform(*DELAY_BETWEEN_SITES))
                    site_info = scrape_website_full(
                        lead["Website"],
                        lead.get("Business Name", ""),
                        city,
                        skip_owner=bool(lead.get("Contact Name")),
                    )
                    lead.update({k: v for k, v in site_info.items() if v})

                log(f"    [{i+1}] {lead['Business Name']} — {lead.get('Phone','')} — {lead.get('Email','')} — {lead.get('Contact Name','')}")
                leads.append(lead)

            except Exception as e:
                log(f"    Error on listing {i+1}: {e}")
                continue

    except Exception as e:
        log(f"  ERROR scraping {query}: {e}")

    return leads

def _owner_from_maps_page(page) -> str:
    """
    While on a Maps listing, try to find the owner name from:
    - Owner response text under reviews (signed '- Name, Owner')
    - The 'Questions & Answers' section
    - The 'About' tab description
    """
    try:
        # Click the "Reviews" tab to load them if not visible
        for label in ["Reviews", "Overview"]:
            try:
                btn = page.locator(f'button[aria-label*="{label}"], [role="tab"]:has-text("{label}")').first
                if btn.is_visible(timeout=1500):
                    btn.click()
                    time.sleep(1)
                    break
            except Exception:
                pass

        html = page.content()
        text = BeautifulSoup(html, "lxml").get_text(" ", strip=True)

        # Pattern: "- Mike, Owner" or "Thanks! – Jane, Founder"
        for m in re.finditer(
            r'[-–]\s*([A-Z][a-z]{1,15}(?:\s+[A-Z][a-z]{1,15})?)\s*,?\s*(?:owner|founder|ceo|president|operator)',
            text, re.IGNORECASE
        ):
            candidate = m.group(1).strip()
            if _valid_name(candidate):
                return candidate

        # Pattern: "Name, Owner at BusinessName" or "Owner • Name"
        name = _extract_name_near_title(text)
        if name:
            return name

    except Exception:
        pass
    return ""


def _extract_listing(page) -> dict:
    """Extract data from an open Google Maps listing page."""
    lead = {}

    html = page.content()
    soup = BeautifulSoup(html, "lxml")

    # Business name
    try:
        name_el = page.query_selector('h1')
        if name_el:
            lead["Business Name"] = name_el.inner_text().strip()
    except Exception:
        pass

    # Phone: prefer tel: links (most reliable on Maps pages)
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("tel:"):
            phone = href.replace("tel:", "").strip()
            # Basic sanity: must have 10+ digits
            digits = re.sub(r"\D", "", phone)
            if len(digits) >= 10:
                lead["Phone"] = phone
            break
    # Fallback to strict regex
    if not lead.get("Phone"):
        pm = PHONE_RE.search(html)
        if pm:
            lead["Phone"] = pm.group(0).strip()

    # Website: Google Maps puts it in <a data-item-id="authority"> or aria-label="Website"
    for selector in [
        'a[data-item-id="authority"]',
        'a[aria-label*="Website"]',
        'a[aria-label*="website"]',
    ]:
        try:
            wb = page.query_selector(selector)
            if wb:
                href = wb.get_attribute("href") or ""
                # Google redirects via /url?q= — decode it
                if "/url?q=" in href:
                    from urllib.parse import unquote, urlparse, parse_qs
                    qs = parse_qs(urlparse(href).query)
                    href = qs.get("q", [href])[0]
                clean = href.split("?")[0] if "?" in href else href
                if clean.startswith("http") and "google" not in clean:
                    lead["Website"] = clean
                    break
        except Exception:
            pass

    # Fallback: look for external links in the page
    if not lead.get("Website"):
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            aria = a.get("aria-label", "").lower()
            if ("website" in aria) or ("http" in href and "google.com" not in href and
                    "maps" not in href and href.startswith("http")):
                clean = href.split("?")[0]
                if "google" not in clean and len(clean) > 10:
                    lead["Website"] = clean
                    break

    return lead

# ── Main ───────────────────────────────────────────────────────────────────
def build_queue(queue_file=None):
    """
    Return list of (btype, city, state, country, timezone) tuples to process.
    If queue_file is given, load that specific list. Otherwise use full CITIES x TYPES.
    """
    progress = load_progress()
    done_combos = set(progress.get("done", []))

    if queue_file and Path(queue_file).exists():
        with open(queue_file) as f:
            items = json.load(f)
        queue = []
        for item in items:
            key = combo_key(item["type"], item["city"])
            # Always run queue file items (CLI may have already filtered done ones)
            queue.append((item["type"], item["city"], item["state"], item["country"], item["timezone"]))
        return queue, done_combos, progress

    # Default: all remaining from full matrix
    queue = []
    for city_tuple in CITIES:
        city, state, country, timezone = city_tuple
        for btype in BUSINESS_TYPES:
            if combo_key(btype, city) not in done_combos:
                queue.append((btype, city, state, country, timezone))
    return queue, done_combos, progress


def main():
    import argparse as _ap
    parser = _ap.ArgumentParser()
    parser.add_argument("--queue", help="Path to JSON queue file from cli.py run command")
    args = parser.parse_args()

    log("=" * 60)
    log("Lead Finder starting")
    if args.queue:
        log(f"Mode: targeted queue — {args.queue}")
    else:
        log("Mode: full scan (all remaining combos)")
    log(f"Output CSV: {OUTPUT_CSV}")
    log(f"Progress file: {PROGRESS_FILE}")
    log("=" * 60)

    init_csv()
    queue, done_combos, progress = build_queue(args.queue)
    total_leads = progress.get("total_leads", 0)

    log(f"Combos to run: {len(queue)} | Leads so far: {total_leads}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ]
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=HEADERS["User-Agent"],
            locale="en-US",
        )
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)
        # Pre-set Google consent cookie so EU servers never show the consent page.
        # SOCS is the cookie Google checks; this value encodes "accept all".
        context.add_cookies([{
            "name": "SOCS",
            "value": "CAESEwgDEgk0OTY4MTQ0NDAYASAB",
            "domain": ".google.com",
            "path": "/",
            "sameSite": "Lax",
        }])
        page = context.new_page()

        try:
            for btype, city, state, country, timezone in queue:
                key = combo_key(btype, city)

                log(f"\n{'─'*50}")
                log(f"City: {city}, {state} | Type: {btype}")

                leads = scrape_maps(page, btype, city, state, country, timezone)

                if leads:
                    append_leads(leads)
                    total_leads += len(leads)
                    log(f"  Saved {len(leads)} leads (total: {total_leads})")

                done_combos.add(key)
                progress["done"] = list(done_combos)
                progress["total_leads"] = total_leads
                save_progress(progress)

                delay = random.uniform(*DELAY_BETWEEN_SEARCHES)
                log(f"  Waiting {delay:.1f}s before next search...")
                time.sleep(delay)

        except KeyboardInterrupt:
            log("\nInterrupted. Progress saved.")
        finally:
            browser.close()

    # Clean up queue file after run
    if args.queue and Path(args.queue).exists():
        Path(args.queue).unlink()

    log(f"\nDone. Total leads: {total_leads}")
    log(f"Output: {OUTPUT_CSV}")

if __name__ == "__main__":
    main()
