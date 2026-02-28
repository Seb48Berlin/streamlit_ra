import streamlit as st
import requests
import re
import json
import os
from datetime import datetime, timedelta
import pytz

st.set_page_config(page_title="RA Berlin Free Events – Mar 2026", page_icon="🎵", layout="wide")

BERLIN_TZ = pytz.timezone("Europe/Berlin")
ALLOWED_HOURS = {11, 16, 21}
CACHE_FILE = "ra_events_cache.json"

MONTH_ORDER = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

SYSTEM_PROMPT = """You are a data extraction assistant. The user will give you a search query.
Search the web for it and return ONLY a JSON array (no markdown, no explanation) of events found.
Each item must have exactly these fields:
  - title: event title, with "free entry" (any case/brackets) removed
  - url: the ra.co event URL
  - date_display: date in format "D Mon" e.g. "8 Mar" — extracted from the subheading/snippet, removing everything before and including any dash (—, –)
  - date_sort: integer in format MMDD e.g. 308 for March 8
  - subtitle: the snippet text after the date, cleaned up

Only include events from ra.co/events URLs. Skip any RA homepage, navigation, or non-event links.
Return only valid JSON array. No markdown fences."""


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_now():
    return datetime.now(BERLIN_TZ)


def slot_label(dt):
    """Unique string per allowed slot: '20260308_11'"""
    return dt.strftime("%Y%m%d_") + str(dt.hour)


def current_slot(now):
    """Return slot label if we're in an allowed hour, else None."""
    if now.hour in ALLOWED_HOURS:
        return slot_label(now)
    return None


def next_slot(now):
    for h in sorted(ALLOWED_HOURS):
        candidate = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if candidate > now:
            return candidate
    tomorrow = (now + timedelta(days=1)).replace(
        hour=min(ALLOWED_HOURS), minute=0, second=0, microsecond=0
    )
    return tomorrow


def remove_free_entry(text):
    text = re.sub(r'[\(\[]\s*free\s*entry\s*[\)\]]', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\bfree\s*entry\b', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s{2,}', ' ', text).strip().strip(',').strip('–').strip('-').strip()
    return text


def clean_subheading(text):
    for dash in ['—', '–']:
        if dash in text:
            return text.split(dash, 1)[1].strip()
    return text


def parse_date(sub):
    m = re.search(r'(\d{1,2})\s+([A-Za-z]{3})', sub)
    if m:
        day = int(m.group(1))
        mon_num = MONTH_ORDER.get(m.group(2).lower(), 99)
        return mon_num * 100 + day, "{} {}".format(day, m.group(2).capitalize())
    return 9999, sub


# ── Persistent file cache ─────────────────────────────────────────────────────

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"slot": None, "events": [], "fetched_at": None, "fetch_count": 0, "fetch_log": []}


def save_cache(data):
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        st.warning("Could not save cache: {}".format(e))


# ── API fetchers ──────────────────────────────────────────────────────────────

def fetch_via_anthropic(api_key):
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 4000,
        "system": SYSTEM_PROMPT,
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        "messages": [{"role": "user", "content": 'site:ra.co/events "Berlin" "Free Entry" "Mar" "2026"'}],
    }
    try:
        resp = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload, timeout=60)
        resp.raise_for_status()
        full_text = "".join(b.get("text", "") for b in resp.json().get("content", []) if b.get("type") == "text")
        clean = re.sub(r'```json|```', '', full_text).strip()
        m = re.search(r'\[.*\]', clean, re.DOTALL)
        if m:
            return json.loads(m.group(0)), None
        return [], "No JSON array in response. Raw: {}".format(full_text[:300])
    except Exception as e:
        return None, str(e)


def fetch_via_serpapi(serpapi_key):
    params = {
        "engine": "google",
        "q": 'site:ra.co/events "Berlin" "Free Entry" "Mar" "2026"',
        "api_key": serpapi_key,
        "num": 20,
    }
    try:
        resp = requests.get("https://serpapi.com/search", params=params, timeout=30)
        resp.raise_for_status()
        events = []
        for r in resp.json().get("organic_results", []):
            href = r.get("link", "")
            if "ra.co/events" not in href:
                continue
            raw_title = re.sub(r'\s*[⟋|]\s*RA\s*$', '', r.get("title", "")).strip()
            clean_title = remove_free_entry(raw_title)
            cleaned_sub = clean_subheading(r.get("snippet", ""))
            sort_val, date_display = parse_date(cleaned_sub)
            m = re.search(r'\d{1,2}\s+[A-Za-z]{3}', cleaned_sub)
            subtitle = cleaned_sub[m.end():].strip().lstrip('·').strip() if m else cleaned_sub
            events.append({"title": clean_title, "url": href, "date_display": date_display,
                           "date_sort": sort_val, "subtitle": subtitle})
        events.sort(key=lambda x: x["date_sort"])
        return events, None
    except Exception as e:
        return None, str(e)


def normalize_events(raw):
    events = []
    for ev in raw:
        ds = ev.get("date_sort", 9999)
        try:
            ds = int(ds)
        except (ValueError, TypeError):
            ds = 9999
        events.append({
            "title": remove_free_entry(ev.get("title", "")),
            "url": ev.get("url", ev.get("href", "")),
            "date_display": ev.get("date_display", ""),
            "date_sort": ds,
            "subtitle": ev.get("subtitle", ev.get("subheading", "")),
        })
    events.sort(key=lambda x: x["date_sort"])
    return events


# ── UI ────────────────────────────────────────────────────────────────────────

st.title("🎵 RA Berlin — Free Entry · Mar 2026")

now = get_now()
cache = load_cache()
no_cache_yet = not cache.get("events")
in_slot = now.hour in ALLOWED_HOURS
this_slot = slot_label(now) if in_slot else None
nxt = next_slot(now)
delta = nxt - now
h, m_left = divmod(int(delta.total_seconds() // 60), 60)

with st.sidebar:
    st.header("⚙️ Configuration")

    backend = st.radio("Backend", ["Anthropic API (Claude + web search)", "SerpAPI (Google)"], index=0)

    if "Anthropic" in backend:
        api_key = st.text_input("Anthropic API Key", type="password", placeholder="sk-ant-...",
                                help="console.anthropic.com")
        serpapi_key = None
    else:
        api_key = None
        serpapi_key = st.text_input("SerpAPI Key", type="password", placeholder="your serpapi key",
                                    help="Free tier: serpapi.com")

    st.markdown("---")
    st.markdown("**🕐 Berlin time:** `{}`".format(now.strftime("%H:%M")))

    if in_slot:
        st.success("✅ Fetch window open ({}:00)".format(now.hour))
    else:
        st.warning("⏳ Next fetch: **{}** ({} h {} min)".format(nxt.strftime("%H:%M"), h, m_left))

    st.markdown("---")
    budget = 93
    used = cache.get("fetch_count", 0)
    st.markdown("**📊 SerpAPI budget**")
    st.progress(min(used / budget, 1.0))
    st.caption("{} / {} used · {} remaining".format(used, budget, budget - used))

    if cache.get("fetch_log"):
        with st.expander("Fetch history"):
            for entry in reversed(cache["fetch_log"][-10:]):
                st.caption(entry)

    fetch_btn = st.button("🔍 Fetch Now", use_container_width=True, disabled=(not in_slot and not no_cache_yet))
    if not in_slot and not no_cache_yet:
        st.caption("Unlocks at {}:00".format(nxt.strftime("%H")))

# ── Auto-fetch logic: fetch if in slot and not yet fetched this slot ──────────

no_cache_yet = not cache.get("events")

should_fetch = False
if no_cache_yet:
    should_fetch = True  # first start — always fetch regardless of time
elif in_slot and this_slot != cache.get("slot"):
    should_fetch = True  # new slot, auto-fetch
if fetch_btn:
    should_fetch = True  # manual override

if should_fetch:
    if "Anthropic" in backend and api_key:
        with st.spinner("Fetching via Claude + web search…"):
            raw, error = fetch_via_anthropic(api_key)
    elif "SerpAPI" in backend and serpapi_key:
        with st.spinner("Fetching via SerpAPI…"):
            raw, error = fetch_via_serpapi(serpapi_key)
    else:
        raw, error = None, None
        st.info("Enter your API key in the sidebar to fetch fresh results.")

    if error:
        st.error("Fetch error: {}".format(error))
    elif raw is not None:
        normalized = normalize_events(raw)
        log_entry = "[{}] {} event(s) via {}".format(
            now.strftime("%d %b %H:%M"),
            len(normalized),
            "Anthropic" if "Anthropic" in backend else "SerpAPI"
        )
        cache["slot"] = this_slot
        cache["events"] = normalized
        cache["fetched_at"] = now.strftime("%d %b %H:%M")
        cache["fetch_count"] = cache.get("fetch_count", 0) + 1
        logs = cache.get("fetch_log", [])
        logs.append(log_entry)
        cache["fetch_log"] = logs
        save_cache(cache)
        st.rerun()

# ── Display cached results (always shown if available) ────────────────────────

events = cache.get("events", [])
fetched_at = cache.get("fetched_at")

if not events:
    if in_slot:
        st.info("No cached results yet. Enter your API key and click **Fetch Now**.")
    else:
        st.info(
            "🕐 **Next fetch: {} Berlin time** ({} h {} min)\n\n"
            "Fetches run at **11:00, 16:00 and 21:00** to stay within the 100/month budget. "
            "Last cached results will always be shown below once available.".format(
                nxt.strftime("%H:%M"), h, m_left
            )
        )
else:
    st.success("**{}** event(s) — last updated {}{}".format(
        len(events),
        fetched_at or "unknown",
        " *(cached)*" if not in_slot or this_slot == cache.get("slot") else ""
    ))
    st.divider()

    for ev in events:
        col1, col2 = st.columns([1, 6])
        with col1:
            if ev["date_display"]:
                st.markdown(
                    '<div style="background:#1a1a2e;color:#e0e0ff;border-radius:8px;'
                    'padding:8px 4px;text-align:center;font-weight:700;font-size:1rem;">'
                    '{}</div>'.format(ev["date_display"]),
                    unsafe_allow_html=True
                )
        with col2:
            st.markdown(
                '<a href="{}" target="_blank" style="font-size:1.05rem;font-weight:600;'
                'text-decoration:none;color:#4f8ef7;">{}</a>'.format(ev["url"], ev["title"]),
                unsafe_allow_html=True
            )
            if ev["subtitle"]:
                st.caption(ev["subtitle"])
        st.divider()
