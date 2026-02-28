import streamlit as st
import requests
from bs4 import BeautifulSoup
import re

st.set_page_config(page_title="RA Berlin Free Events – Mar 2026", page_icon="🎵", layout="wide")

SEARCH_URL = (
    "https://www.google.com/search"
    "?q=site%3Ara.co%2Fevents+%22Berlin%22+%22Free+Entry%22+%22Mar%22+%222026%22&num=20"
)

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


def remove_free_entry(text):
    text = re.sub(r'[\(\[]\s*free\s*entry\s*[\)\]]', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\bfree\s*entry\b', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s{2,}', ' ', text).strip().strip(',').strip('–').strip('-').strip()
    return text


def clean_subheading(text):
    for dash in ['—', '–']:
        if dash in text:
            text = text.split(dash, 1)[1].strip()
            return text
    return text


def parse_date(sub):
    m = re.search(r'(\d{1,2})\s+([A-Za-z]{3})', sub)
    if m:
        day = int(m.group(1))
        mon_str = m.group(2).lower()
        mon_num = MONTH_ORDER.get(mon_str, 99)
        return (mon_num, day), "{} {}".format(day, m.group(2).capitalize())
    return (99, 99), sub


def format_subheading(raw):
    cleaned = clean_subheading(raw)
    sort_key, date_label = parse_date(cleaned)
    m = re.search(r'\d{1,2}\s+[A-Za-z]{3}', cleaned)
    if m:
        rest = cleaned[m.end():].strip().lstrip('·').lstrip(',').strip()
        display = "{} · {}".format(date_label, rest) if rest else date_label
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

    soup = BeautifulSoup(resp.text, "lxml")
    results = []

    for block in soup.select("div.g, div.Gx5Zad, div[data-sokoban-container]"):
        title_tag = block.find("h3")
        link_tag = block.find("a", href=True)
        if not title_tag or not link_tag:
            continue

        href = link_tag.get("href", "")
        if href.startswith("/url?q="):
            href = re.sub(
                r'/url\?q=([^&]+).*',
                lambda mx: requests.utils.unquote(mx.group(1)),
                href
            )

        if "ra.co" not in href:
            continue

        raw_title = title_tag.get_text(" ", strip=True)

        if len(raw_title) < 5 or raw_title.lower() in ("ra", "resident advisor"):
            continue

        raw_title = re.sub(r'\s*[⟋|]\s*RA\s*$', '', raw_title, flags=re.IGNORECASE).strip()
        clean_title = remove_free_entry(raw_title)

        snippet_tag = block.find(
            "div",
            class_=re.compile(r"(IsZvec|VwiC3b|s3v9rd|BNeawe|yDYNvb|MUxGbd)")
        )
        raw_sub = snippet_tag.get_text(" ", strip=True) if snippet_tag else ""

        display_sub, sort_key = format_subheading(raw_sub) if raw_sub else ("", (99, 99))

        results.append({
            "title": clean_title,
            "href": href,
            "subheading": display_sub,
            "sort_key": sort_key,
        })

    seen = set()
    unique = []
    for r in results:
        if r["href"] not in seen:
            seen.add(r["href"])
            unique.append(r)

    unique.sort(key=lambda x: x["sort_key"])
    return unique, None


# ── UI ────────────────────────────────────────────────────────────────────────

st.title("🎵 RA Berlin — Free Entry Events · Mar 2026")
st.caption("Source: [Google Search]({})".format(SEARCH_URL))

with st.spinner("Fetching events from Google…"):
    events, error = fetch_events()

if error:
    st.error("Could not fetch results: {}".format(error))
    st.info("Google may block automated requests. Try running locally or use a residential proxy.")
elif not events:
    st.warning("No RA events found. Google may have returned a CAPTCHA or changed its HTML.")
    st.markdown("[Open search in browser]({})".format(SEARCH_URL))
else:
    st.success("Found **{}** event(s)".format(len(events)))
    st.divider()

    for ev in events:
        title_html = (
            '<a href="{}" target="_blank" '
            'style="font-size:1.1rem;font-weight:600;text-decoration:none;">'
            '{}</a>'
        ).format(ev["href"], ev["title"])
        st.markdown(title_html, unsafe_allow_html=True)
        if ev["subheading"]:
            st.caption(ev["subheading"])
        st.divider()
