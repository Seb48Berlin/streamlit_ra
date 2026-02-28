import streamlit as st
import requests
from bs4 import BeautifulSoup
import re
from datetime import datetime

st.set_page_config(page_title="RA Berlin Free Events – Mar 2026", page_icon="🎵", layout="wide")

SEARCH_URL = "https://www.google.com/search?q=site%3Ara.co%2Fevents+%22Berlin%22+%22Free+Entry%22+%22Mar%22+%222026%22&num=20"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

MONTH_ORDER = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

def remove_free_entry(text: str) -> str:
    """Remove 'free entry' (case-insensitive) and surrounding brackets/spaces."""
    # Remove bracketed variants like (Free Entry) or [Free Entry]
    text = re.sub(r'[\(\[]\s*free\s*entry\s*[\)\]]', '', text, flags=re.IGNORECASE)
    # Remove standalone occurrences
    text = re.sub(r'\bfree\s*entry\b', '', text, flags=re.IGNORECASE)
    # Clean up double spaces and trailing/leading commas or dashes
    text = re.sub(r'\s{2,}', ' ', text).strip().strip(',').strip('–').strip('-').strip()
    return text

def clean_subheading(text: str) -> str:
    """Remove everything up to and including '—' (em dash) and surrounding spaces."""
    # Handle various dash types: —, –, -
    for dash in ['—', '–']:
        if dash in text:
            parts = text.split(dash, 1)
            text = parts[1].strip()
            return text
    return text

def parse_date_from_subheading(sub: str):
    """Try to extract a sortable date from subheading text like 'Sat, 8 Mar 2026 · ...'"""
    # Pattern: optional weekday, day number, 3-letter month
    m = re.search(r'(\d{1,2})\s+([A-Za-z]{3})', sub)
    if m:
        day = int(m.group(1))
        mon_str = m.group(2).lower()
        mon_num = MONTH_ORDER.get(mon_str, 99)
        return (mon_num, day), f"{day} {m.group(2).capitalize()}"
    return (99, 99), sub

def format_subheading(raw: str) -> tuple[str, tuple]:
    """Clean subheading and return (display_string, sort_key)."""
    cleaned = clean_subheading(raw)
    sort_key, date_label = parse_date_from_subheading(cleaned)
    # Rebuild: date_label + rest after date
    m = re.search(r'\d{1,2}\s+[A-Za-z]{3}', cleaned)
    if m:
        rest = cleaned[m.end():].strip().lstrip('·').lstrip(',').strip()
        display = f"{date_label}{' · ' + rest if rest else ''}"
    else:
        display = cleaned
    return display, sort_key

@st.cache_data(ttl=300)
def fetch_events():
    try:
        resp = requests.get(SEARCH_URL, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        return None, str(e)

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []

    # Google search results: each organic result is in a div with class 'g' or similar
    # We look for result blocks containing titles + links + snippets
    for block in soup.select("div.g, div[data-sokoban-container], div.Gx5Zad"):
        # Title link
        title_tag = block.find("h3")
        link_tag = block.find("a", href=True)
        if not title_tag or not link_tag:
            continue

        href = link_tag.get("href", "")
        # Clean Google redirect URLs
        if href.startswith("/url?q="):
            href = re.sub(r'/url\?q=([^&]+).*', lambda m: requests.utils.unquote(m.group(1)), href)

        # Skip non-RA links
        if "ra.co" not in href:
            continue

        raw_title = title_tag.get_text(" ", strip=True)

        # Skip RA logo / nav links (very short or generic)
        if len(raw_title) < 5 or raw_title.lower() in ("ra", "resident advisor"):
            continue

        # Remove "⟋ RA" suffix that RA appends
        raw_title = re.sub(r'\s*[⟋|]\s*RA\s*$', '', raw_title, flags=re.IGNORECASE).strip()

        # Remove "Free Entry" from title
        clean_title = remove_free_entry(raw_title)

        # Subheading / snippet
        snippet_tag = block.find("div", class_=re.compile(r"(IsZvec|VwiC3b|s3v9rd|st|BNeawe)"))
        raw_sub = snippet_tag.get_text(" ", strip=True) if snippet_tag else ""

        display_sub, sort_key = format_subheading(raw_sub) if raw_sub else ("", (99, 99))

        results.append({
            "title": clean_title,
            "href": href,
            "subheading": display_sub,
            "sort_key": sort_key,
        })

    # Remove duplicates by href
    seen = set()
    unique = []
    for r in results:
        if r["href"] not in seen:
            seen.add(r["href"])
            unique.append(r)

    # Sort by date
    unique.sort(key=lambda x: x["sort_key"])
    return unique, None

# ── UI ────────────────────────────────────────────────────────────────────────

st.title("🎵 RA Berlin — Free Entry Events · Mar 2026")
st.caption(f"Source: [Google Search]({SEARCH_URL})")

with st.spinner("Fetching events from Google…"):
    events, error = fetch_events()

if error:
    st.error(f"Could not fetch results: {error}")
    st.info(
        "Google may block automated requests. "
        "Try opening the app in a browser that is already logged into Google, "
        "or run it locally where your IP isn't rate-limited."
    )
elif not events:
    st.warning(
        "No RA events found. Google may have returned a CAPTCHA or changed its HTML structure. "
        "Try refreshing or opening the link manually."
    )
    st.markdown(f"[Open search in browser]({SEARCH_URL})")
else:
    st.success(f"Found **{len(events)}** event(s)")
    st.divider()

    for ev in events:
        title_html = f'<a href="{ev["href"]}" target="_blank" style="font-size:1.1rem;font-weight:600;text-decoration:none;">{ev["title"]}</a>'
        st.markdown(title_html, unsafe_allow_html=True)
        if ev["subheading"]:
            st.caption(ev["subheading"])
        st.divider()
