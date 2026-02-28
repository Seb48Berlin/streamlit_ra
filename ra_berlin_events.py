import streamlit as st
import requests
import re
import json
import os
from datetime import datetime, timedelta
import pytz

st.set_page_config(page_title="Techno Berlin Free Entry", page_icon="🎵", layout="wide")

BERLIN_TZ = pytz.timezone("Europe/Berlin")
ALLOWED_HOURS = {11, 16, 21}
CACHE_FILE = "ra_events_cache.json"
ADMIN_PASSWORD = "admin1234"  # change this

MONTH_ORDER = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_now():
    return datetime.now(BERLIN_TZ)


def get_search_months(now):
    """Return (current_month_abbr, next_month_abbr, year_str) for search query."""
    this = now.replace(day=1)
    nxt = (this + timedelta(days=32)).replace(day=1)
    return this.strftime("%b"), nxt.strftime("%b"), now.strftime("%Y"), nxt.strftime("%Y")


def slot_label(dt):
    return dt.strftime("%Y%m%d_") + str(dt.hour)


def next_slot(now):
    for h in sorted(ALLOWED_HOURS):
        candidate = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if candidate > now:
            return candidate
    return (now + timedelta(days=1)).replace(
        hour=min(ALLOWED_HOURS), minute=0, second=0, microsecond=0
    )


def remove_noise(text):
    """Remove 'free entry', bracketed * variants, extra spaces."""
    # Remove (*free entry*), [free entry], (free entry), * free entry * etc.
    text = re.sub(r'[\(\[]\s*\*?\s*free\s*entry\s*\*?\s*[\)\]]', '', text, flags=re.IGNORECASE)
    # Remove standalone free entry with optional surrounding *
    text = re.sub(r'\*?\s*\bfree\s*entry\b\s*\*?', '', text, flags=re.IGNORECASE)
    # Remove leftover asterisks
    text = re.sub(r'\*', '', text)
    # Clean multiple spaces / stray punctuation
    text = re.sub(r'\s{2,}', ' ', text)
    text = text.strip().strip(',').strip('–').strip('-').strip()
    return text


def clean_subheading(text):
    """Remove everything before and including the first em/en dash."""
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
    return 9999, ""


# ── Persistent file cache ─────────────────────────────────────────────────────

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"slot": None, "events": [], "fetched_at": None, "fetch_count": 0, "fetch_log": [],
            "api_key": "", "serpapi_key": "", "backend": "SerpAPI (Google)"}


def save_cache(data):
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        st.warning("Could not save cache: {}".format(e))


# ── API fetchers ──────────────────────────────────────────────────────────────

def build_queries(now):
    m1, m2, y1, y2 = get_search_months(now)
    queries = [
        'site:ra.co/events "Berlin" "Free Entry" "{}" "{}"'.format(m1, y1),
    ]
    if m2 != m1:
        queries.append('site:ra.co/events "Berlin" "Free Entry" "{}" "{}"'.format(m2, y2))
    return queries


def fetch_via_serpapi(serpapi_key, now):
    queries = build_queries(now)
    all_events = []
    seen_urls = set()
    errors = []
    for q in queries:
        params = {"engine": "google", "q": q, "api_key": serpapi_key, "num": 20}
        try:
            resp = requests.get("https://serpapi.com/search", params=params, timeout=30)
            resp.raise_for_status()
            for r in resp.json().get("organic_results", []):
                href = r.get("link", "")
                if "ra.co/events" not in href or href in seen_urls:
                    continue
                seen_urls.add(href)
                raw_title = re.sub(r'\s*[⟋|]\s*RA\s*$', '', r.get("title", "")).strip()
                clean_title = remove_noise(raw_title)
                raw_snippet = r.get("snippet", "")
                # Full subheading: strip before dash, keep rest intact
                full_sub = clean_subheading(raw_snippet)
                sort_val, date_display = parse_date(full_sub)
                all_events.append({
                    "title": clean_title,
                    "url": href,
                    "date_display": date_display,
                    "date_sort": sort_val,
                    "subtitle": full_sub,
                })
        except Exception as e:
            errors.append(str(e))
    all_events.sort(key=lambda x: x["date_sort"])
    return all_events, (", ".join(errors) if errors else None)


def fetch_via_anthropic(api_key, now):
    m1, m2, y1, y2 = get_search_months(now)
    months_str = '"{}" "{}"'.format(m1, y1)
    if m2 != m1:
        months_str += ' OR "{}" "{}"'.format(m2, y2)
    system_prompt = """You are a data extraction assistant. Search the web and return ONLY a JSON array of Berlin techno free entry events from ra.co.
Each item must have:
  - title: event name with "free entry" (any case/brackets/asterisks) fully removed
  - url: the ra.co/events URL
  - date_display: date as "D Mon" e.g. "8 Mar"
  - date_sort: integer MMDD e.g. 308
  - subtitle: the FULL snippet/subheading text after any dash, unchanged
Only ra.co/events URLs. No markdown. Return valid JSON array only."""

    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    payload = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 4000,
        "system": system_prompt,
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        "messages": [{"role": "user", "content": 'site:ra.co/events "Berlin" "Free Entry" {}'.format(months_str)}],
    }
    try:
        resp = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload, timeout=60)
        resp.raise_for_status()
        full_text = "".join(b.get("text", "") for b in resp.json().get("content", []) if b.get("type") == "text")
        clean = re.sub(r'```json|```', '', full_text).strip()
        match = re.search(r'\[.*\]', clean, re.DOTALL)
        if match:
            raw = json.loads(match.group(0))
            events = []
            for ev in raw:
                ds = ev.get("date_sort", 9999)
                try:
                    ds = int(ds)
                except (ValueError, TypeError):
                    ds = 9999
                events.append({
                    "title": remove_noise(ev.get("title", "")),
                    "url": ev.get("url", ""),
                    "date_display": ev.get("date_display", ""),
                    "date_sort": ds,
                    "subtitle": ev.get("subtitle", ""),
                })
            events.sort(key=lambda x: x["date_sort"])
            return events, None
        return [], "No JSON array in response: {}".format(full_text[:300])
    except Exception as e:
        return None, str(e)


# ── SESSION STATE ─────────────────────────────────────────────────────────────

if "admin" not in st.session_state:
    st.session_state.admin = False

# ── MAIN ──────────────────────────────────────────────────────────────────────

now = get_now()
cache = load_cache()
no_cache_yet = not cache.get("events")
in_slot = now.hour in ALLOWED_HOURS
this_slot = slot_label(now) if in_slot else None
nxt = next_slot(now)
delta = nxt - now
h_left, m_left = divmod(int(delta.total_seconds() // 60), 60)

# ── SIDEBAR (admin only) ──────────────────────────────────────────────────────

with st.sidebar:
    if not st.session_state.admin:
        st.markdown("### 🔐 Admin Login")
        pw = st.text_input("Password", type="password")
        if st.button("Login"):
            if pw == ADMIN_PASSWORD:
                st.session_state.admin = True
                st.rerun()
            else:
                st.error("Wrong password")
    else:
        st.markdown("### ⚙️ Admin Panel")
        if st.button("🚪 Logout"):
            st.session_state.admin = False
            st.rerun()

        st.markdown("---")
        backend = st.radio("Backend", ["SerpAPI (Google)", "Anthropic API (Claude + web search)"], index=0)

        if "Anthropic" in backend:
            api_key = st.text_input("Anthropic API Key", value=cache.get("api_key", ""),
                                    type="password", placeholder="sk-ant-...")
            serpapi_key = None
        else:
            api_key = None
            serpapi_key = st.text_input("SerpAPI Key", value=cache.get("serpapi_key", ""),
                                        type="password", placeholder="your serpapi key")

        # Persist keys to cache so auto-fetch works after restart
        if api_key and api_key != cache.get("api_key"):
            cache["api_key"] = api_key
            cache["backend"] = backend
            save_cache(cache)
        if serpapi_key and serpapi_key != cache.get("serpapi_key"):
            cache["serpapi_key"] = serpapi_key
            cache["backend"] = backend
            save_cache(cache)

        st.markdown("---")
        st.markdown("**🕐 Berlin:** `{}`".format(now.strftime("%H:%M")))
        if in_slot:
            st.success("✅ Fetch window open")
        else:
            st.warning("⏳ Next: **{}** ({} h {} min)".format(nxt.strftime("%H:%M"), h_left, m_left))

        m1, m2, y1, y2 = get_search_months(now)
        st.markdown("**🔍 Searching:** {} {} + {} {}".format(m1, y1, m2, y2))

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

        fetch_btn = st.button("🔍 Fetch Now", use_container_width=True,
                              disabled=(not in_slot and not no_cache_yet))
        if not in_slot and not no_cache_yet:
            st.caption("Auto-fetches at 11:00, 16:00, 21:00")
    # end admin block

# ── Resolve backend/keys from cache if not admin ──────────────────────────────
if not st.session_state.admin:
    backend = cache.get("backend", "SerpAPI (Google)")
    api_key = cache.get("api_key", "")
    serpapi_key = cache.get("serpapi_key", "")
    fetch_btn = False  # non-admins can never trigger fetch

# ── Auto-fetch ────────────────────────────────────────────────────────────────

should_fetch = False
if no_cache_yet:
    should_fetch = True
elif in_slot and this_slot != cache.get("slot"):
    should_fetch = True
if st.session_state.admin and fetch_btn:
    should_fetch = True

if should_fetch:
    has_key = (api_key and "Anthropic" in backend) or (serpapi_key and "SerpAPI" in backend)
    if not has_key:
        if no_cache_yet:
            st.info("👈 Log in as admin and enter your API key to load events for the first time.")
    else:
        with st.spinner("Fetching events…"):
            if "Anthropic" in backend:
                raw, error = fetch_via_anthropic(api_key, now)
            else:
                raw, error = fetch_via_serpapi(serpapi_key, now)

        if error:
            st.error("Fetch error: {}".format(error))
        elif raw is not None:
            log_entry = "[{}] {} event(s) via {}".format(
                now.strftime("%d %b %H:%M"), len(raw),
                "Anthropic" if "Anthropic" in backend else "SerpAPI"
            )
            cache["slot"] = this_slot
            cache["events"] = raw
            cache["fetched_at"] = now.strftime("%d %b %H:%M")
            cache["fetch_count"] = cache.get("fetch_count", 0) + 1
            logs = cache.get("fetch_log", [])
            logs.append(log_entry)
            cache["fetch_log"] = logs
            save_cache(cache)
            st.rerun()

# ── Display ───────────────────────────────────────────────────────────────────

st.title("Techno Berlin Free Entry")

events = cache.get("events", [])
fetched_at = cache.get("fetched_at")

if not events:
    st.info("🕐 No events cached yet. Admin login required to fetch.")
else:
    st.caption("Last updated: {} *(cached)*".format(fetched_at or "unknown"))
    st.divider()

    for ev in events:
        date_str = ev.get("date_display", "")
        subtitle = ev.get("subtitle", "")
        title = ev.get("title", "")
        url = ev.get("url", "")

        # Layout: date pill | clickable title + full subtitle
        col1, col2 = st.columns([1, 7])
        with col1:
            if date_str:
                # Simple clean date badge — just text, no black box
                st.markdown(
                    '<div style="color:#888;font-size:0.85rem;font-weight:600;'
                    'padding-top:4px;text-align:center;">{}</div>'.format(date_str),
                    unsafe_allow_html=True
                )
        with col2:
            st.markdown(
                '<a href="{}" target="_blank" style="font-size:1.05rem;font-weight:600;'
                'text-decoration:none;color:#4f8ef7;">{}</a>'.format(url, title),
                unsafe_allow_html=True
            )
            if subtitle:
                st.markdown(
                    '<div style="color:#aaa;font-size:0.85rem;margin-top:2px;">{}</div>'.format(subtitle),
                    unsafe_allow_html=True
                )
        st.divider()
