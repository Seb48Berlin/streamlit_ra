import streamlit as st
import requests
import re
import json
import os
import hashlib
from datetime import datetime, timedelta
import pytz

st.set_page_config(page_title="Techno Berlin Free Entry", page_icon="🎵", layout="wide", initial_sidebar_state="collapsed")

BERLIN_TZ = pytz.timezone("Europe/Berlin")
ALLOWED_HOURS = {11, 16, 21}
CACHE_FILE = "ra_events_cache.json"
ADMIN_PASSWORD_SALT = "2a0d557037025da91acf624ba115be8a"
ADMIN_PASSWORD_HASH = "8bb5a82a90095fbc0b7beaf8498ee3ae7d95af7764dc19890be04ca907995ad4"

# Blocklist: RA event IDs confirmed as false positives (no free entry)
BLOCKED_EVENT_IDS = set()

# Blocklist: event name keywords — events whose title contains any of these are blocked
BLOCKED_NAME_KEYWORDS = []

MONTH_ORDER = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_now():
    return datetime.now(BERLIN_TZ)


def get_search_months(now):
    """Return (m1, m2, m3, y1, y2, y3) for the current + next 2 months."""
    this = now.replace(day=1)
    nxt = (this + timedelta(days=32)).replace(day=1)
    nxt2 = (nxt + timedelta(days=32)).replace(day=1)
    return (
        this.strftime("%b"), nxt.strftime("%b"), nxt2.strftime("%b"),
        this.strftime("%Y"), nxt.strftime("%Y"), nxt2.strftime("%Y"),
    )


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
    """Remove 'free entry' in all variants: brackets, asterisks, dots, separators."""
    # Remove bracketed/asterisked variants: (free entry), [*free entry*], (*free entry*) etc.
    text = re.sub(r'[\(\[]\s*\*?\s*free\s*(entry|ticket)\s*\*?\s*[\)\]]', ' ', text, flags=re.IGNORECASE)
    # Remove with surrounding separators: · free entry ·, - free entry -, | free entry |
    text = re.sub(r'[·\-–—|]\s*\*?\s*free\s*(entry|ticket)\s*\*?\s*(?=[·\-–—|]|$)', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'(?:^|(?<=[·\-–—|]))\s*\*?\s*free\s*(entry|ticket)\s*\*?\s*[·\-–—|]', ' ', text, flags=re.IGNORECASE)
    # Remove any remaining free entry/ticket with optional asterisks
    text = re.sub(r'\*?\s*free\s*(entry|ticket)\s*\*?', ' ', text, flags=re.IGNORECASE)
    # Remove leftover lone asterisks
    text = re.sub(r'\*', '', text)
    text = re.sub(r'Interested[:.] *\d+', '', text, flags=re.IGNORECASE)
    # Collapse multiple separators: ··, --, · ·, etc.
    text = re.sub(r'([·\-–—|])\s*\1+', r'\1', text)
    text = re.sub(r'\s*[·\-–—|]\s*$', '', text)   # trailing separator
    text = re.sub(r'^\s*[·\-–—|]\s*', '', text)   # leading separator
    # Ensure space between words that got joined (e.g. "BerlinNight")
    text = re.sub(r'([a-z])([A-Z])', r'\1 \2', text)
    # Collapse multiple spaces
    text = re.sub(r'\s{2,}', ' ', text)
    text = text.strip().strip(',').strip()
    return text


def clean_subheading(text):
    """Remove everything before and including the first em/en dash."""
    for dash in ['—', '–']:
        if dash in text:
            return text.split(dash, 1)[1].strip()
    return text


def parse_date(sub):
    """Find a date in either 'D Mon [Year]' or 'Mon D [Year]' format.
    Returns (date_sort, date_display, year) where year=None if not found."""
    # Try "D Mon YYYY" format: e.g. "8 Mar 2026", "23 May 2025"
    for m in re.finditer(r'(\d{1,2})\s+([A-Za-z]{3,9})\s*(\d{4})?', sub):
        day = int(m.group(1))
        mon_str = m.group(2).lower()[:3]
        year = int(m.group(3)) if m.group(3) else None
        mon_num = MONTH_ORDER.get(mon_str)
        if mon_num and 1 <= day <= 31:
            return mon_num * 100 + day, "{} {}".format(day, mon_str.capitalize()), year
    # Try "Mon D YYYY" format: e.g. "Mar 8 2026", "May 23, 2025"
    for m in re.finditer(r'([A-Za-z]{3,9})\s+(\d{1,2})[,\s]*(\d{4})?', sub):
        mon_str = m.group(1).lower()[:3]
        day = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else None
        mon_num = MONTH_ORDER.get(mon_str)
        if mon_num and 1 <= day <= 31:
            return mon_num * 100 + day, "{} {}".format(day, mon_str.capitalize()), year
    return 9999, "", None


# ── Persistent file cache ─────────────────────────────────────────────────────

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"slot": None, "events": [], "fetched_at": None, "fetch_count": 0, "fetch_log": [],
            "api_key": "", "serpapi_key": "", "backend": "SerpAPI (Google)", "blocklist": [], "name_blocklist": [], "allowlist": []}


def save_cache(data):
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        st.warning("Could not save cache: {}".format(e))


# ── API fetchers ──────────────────────────────────────────────────────────────

# Free entry keywords — must appear in the snippet (not just the RA-appended title)
FREE_ENTRY_PATTERNS = re.compile(
    r'free\s*entry|free\s*admission|no\s*cover|eintritt\s*frei|free\s*entrance|gratuit',
    re.IGNORECASE
)

# Paid ticket signals — if these appear in the snippet, it's NOT fully free
PAID_PATTERNS = re.compile(
    r'\b(buy\s*tickets?|get\s*tickets?|\d+[,.]?\d*\s*€|€\s*\d+|ticket\s*price|from\s*€|sold\s*out)\b',
    re.IGNORECASE
)




def snippet_confirms_free_entry(title_raw, snippet_raw, highlighted_words=None):
    """Strict free entry check.
    Requires 'free entry' to appear in the snippet CLOSE TO a date or Berlin/venue
    marker (within 150 chars), proving it refers to THIS event not a sidebar event.
    Also rejects if paid signals appear in the same window.
    """
    NEAR_WINDOW = 150

    for fe_match in FREE_ENTRY_PATTERNS.finditer(snippet_raw):
        start = fe_match.start()
        window = snippet_raw[max(0, start - NEAR_WINDOW): start + NEAR_WINDOW]
        # Must be near a date signal or Berlin/venue to be about this specific event
        near_event = re.search(
            r'\d{1,2}[\s.]+[A-Za-z]{3}|[A-Za-z]{3}[\s.,]+\d{1,2}|Berlin|Venue|\d{4}',
            window, re.IGNORECASE
        )
        if near_event and not PAID_PATTERNS.search(window):
            return True
    return False




def build_queries(now):
    m1, m2, m3, y1, y2, y3 = get_search_months(now)
    months = [(m1, y1), (m2, y2), (m3, y3)]
    free_phrases = ["Free Entry", "Free Ticket", "Free Admission", "Eintritt frei", "freier Eintritt"]
    queries = []
    seen = set()
    for phrase in free_phrases:
        for m, y in months:
            key = (phrase, m, y)
            if key not in seen:
                seen.add(key)
                queries.append('site:ra.co/events "Berlin" "{}" "{}" "{}"'.format(phrase, m, y))
    return queries



def fetch_ra_event_info(event_id):
    """Fetch title and date for a manually allowlisted RA event ID."""
    url = "https://ra.co/events/{}".format(event_id)
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        title = re.search(r'<title>([^<]+)</title>', resp.text)
        title = title.group(1).strip() if title else "Event {}".format(event_id)
        title = re.sub(r'\s*[|⟋].*$', '', title).strip()
        m = re.search(r'"startDate"\s*:\s*"(\d{4})-(\d{2})-(\d{2})', resp.text)
        if m:
            year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
            mon_str = [k for k, v in MONTH_ORDER.items() if v == month][0].capitalize()
            date_display = "{} {}".format(day, mon_str)
            date_sort = month * 100 + day
            return {"title": title, "url": url, "date_display": date_display,
                    "date_sort": date_sort, "date_year": year, "subtitle": "", "manual": True}
    except Exception:
        pass
    return None

def fetch_via_serpapi(serpapi_key, now, cache_blocklist=None, name_blocklist=None, status_placeholder=None):
    queries = build_queries(now)
    verified = []
    seen_urls = set()
    errors = []

    for q in queries:
        params = {"engine": "google", "q": q, "api_key": serpapi_key, "num": 20}
        try:
            resp = requests.get("https://serpapi.com/search", params=params, timeout=30)
            resp.raise_for_status()
            for r in resp.json().get("organic_results", []):
                href = r.get("link", "")
                if "ra.co/events" not in href:
                    continue
                # Normalize: strip query params + locale prefix (de/es/fr.ra.co → ra.co)
                href = re.split(r'[?#]', href)[0]
                href = re.sub(r'https?://[a-z]{2}\.ra\.co', 'https://ra.co', href)
                # Extract numeric event ID, deduplicate by both URL and ID
                parts = href.rstrip("/").split("/")
                event_id = next((p for p in reversed(parts) if p.isdigit()), parts[-1])
                if href in seen_urls or event_id in seen_urls:
                    continue
                all_blocked = BLOCKED_EVENT_IDS | set(cache_blocklist or [])
                if event_id in all_blocked:
                    continue
                seen_urls.add(href)
                seen_urls.add(event_id)

                raw_title = r.get("title", "")
                raw_snippet = r.get("snippet", "")

                # Check name blocklist (hardcoded + admin-added)
                all_name_blocked = BLOCKED_NAME_KEYWORDS + (name_blocklist or [])
                if any(kw.lower() in raw_title.lower() for kw in all_name_blocked if kw.strip()):
                    continue

                # Must be in Berlin — check URL, title AND snippet for Berlin, Germany context
                # Reject if another city appears in title (Vienna, Minneapolis, etc.)
                title_for_city = re.sub(r'\s*[⟋|].*$', '', raw_title)
                if not re.search(r'\bBerlin\b', href + " " + title_for_city + " " + raw_snippet[:300], re.IGNORECASE):
                    continue
                # Reject if a non-Berlin city appears in the VENUE part of the title
                # (after "bei" or "at" or "@") — ignore artist/event name part
                non_berlin_cities = re.compile(
                    r'\b(Vienna|Wien|Minneapolis|Hamburg|Munich|München|Cologne|Köln|Frankfurt|'
                    r'Amsterdam|London|Paris|Barcelona|Madrid|Rome|Roma|Prague|Praha|'
                    r'Warsaw|Warszawa|Budapest|Zurich|Zürich|Brussels|Brüssel|'
                    r'Stockholm|Copenhagen|Oslo|Helsinki|Dublin|Lisbon|Lisboa)\b',
                    re.IGNORECASE
                )
                # Extract venue portion: everything after "bei", "at", "@" separator
                venue_part = re.split(r'\s+(?:bei|at|@)\s+', title_for_city, maxsplit=1)
                venue_str = venue_part[-1] if len(venue_part) > 1 else title_for_city
                if non_berlin_cities.search(venue_str):
                    continue
                highlighted = r.get("snippet_highlighted_words", None)

                # Strict check: free entry must be in THIS event's own text, not sidebar
                if not snippet_confirms_free_entry(raw_title, raw_snippet, highlighted):
                    continue


                clean_title = remove_noise(re.sub(r'\s*[⟋|]\s*RA\s*$', '', raw_title).strip())
                full_sub = remove_noise(clean_subheading(raw_snippet))
                # Parse date from full snippet (not just cleaned sub) to catch dates buried deep
                sort_val, date_display, ev_year = parse_date(raw_snippet)
                if sort_val == 9999:
                    sort_val, date_display, ev_year = parse_date(full_sub)
                verified.append({
                    "title": clean_title,
                    "url": href,
                    "date_display": date_display,
                    "date_sort": sort_val,
                    "date_year": ev_year,
                    "subtitle": full_sub,
                })
        except Exception as e:
            errors.append(str(e))

    # Filter: keep only future events in current+next month and correct year
    now_sort = now.month * 100 + now.day
    current_year = now.year
    m1, m2, m3, y1, y2, y3 = get_search_months(now)
    valid_months = {MONTH_ORDER[m1.lower()[:3]], MONTH_ORDER[m2.lower()[:3]], MONTH_ORDER[m3.lower()[:3]]}

    filtered = []
    for ev in verified:
        ds = ev["date_sort"]
        ev_year = ev.get("date_year")
        if ds == 9999:
            continue  # no date parsed
        # Reject if year is explicitly in the past
        if ev_year is not None and ev_year < current_year:
            continue
        ev_month = ds // 100
        if ev_month not in valid_months:
            continue  # outside search window
        if ds < now_sort:
            continue  # already past this month
        filtered.append(ev)

    filtered.sort(key=lambda x: x["date_sort"])
    return filtered, (", ".join(errors) if errors else None)


def fetch_via_anthropic(api_key, now):
    m1, m2, m3, y1, y2, y3 = get_search_months(now)
    months_str = '"{}" "{}"'.format(m1, y1)
    if m2 != m1:
        months_str += ' OR "{}" "{}"'.format(m2, y2)
    if m3 != m2:
        months_str += ' OR "{}" "{}"'.format(m3, y3)
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
                    "subtitle": remove_noise(ev.get("subtitle", "")),
                })
            events.sort(key=lambda x: x["date_sort"])
            return events, None
        return [], "No JSON array in response: {}".format(full_text[:300])
    except Exception as e:
        return None, str(e)


# ── SESSION STATE ─────────────────────────────────────────────────────────────

if "admin" not in st.session_state:
    st.session_state.admin = False
if "fetch_requested" not in st.session_state:
    st.session_state.fetch_requested = False
st.markdown("<style>[data-testid='collapsedControl']{display:none !important}</style>", unsafe_allow_html=True)

# ── MAIN ──────────────────────────────────────────────────────────────────────

now = get_now()
cache = load_cache()
no_cache_yet = not cache.get("events")
in_slot = now.hour in ALLOWED_HOURS
this_slot = slot_label(now) if in_slot else None
nxt = next_slot(now)
delta = nxt - now
h_left, m_left = divmod(int(delta.total_seconds() // 60), 60)

# ── SIDEBAR — emptied, admin panel is at bottom of page ────────────────────
with st.sidebar:
    st.empty()

# ── Resolve backend/keys from cache ──────────────────────────────────────────
backend = cache.get("backend", "SerpAPI (Google)")
api_key = cache.get("api_key", "")
serpapi_key = cache.get("serpapi_key", "")

# ── Auto-fetch ────────────────────────────────────────────────────────────────

should_fetch = False
if no_cache_yet:
    should_fetch = True
elif in_slot and this_slot != cache.get("slot"):
    should_fetch = True
if st.session_state.fetch_requested:
    should_fetch = True
    st.session_state.fetch_requested = False

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
                raw, error = fetch_via_serpapi(serpapi_key, now, cache_blocklist=cache.get("blocklist", []), name_blocklist=cache.get("name_blocklist", []))

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

# Apply blocklists to cached events at display time
_all_blocked_ids = BLOCKED_EVENT_IDS | set(cache.get("blocklist", []))
_all_blocked_names = BLOCKED_NAME_KEYWORDS + cache.get("name_blocklist", [])
def _is_blocked(ev):
    url_parts = ev.get("url", "").rstrip("/").split("/")
    ev_id = next((p for p in reversed(url_parts) if p.isdigit()), "")
    if ev_id in _all_blocked_ids:
        return True
    title = ev.get("title", "")
    if any(kw.lower() in title.lower() for kw in _all_blocked_names if kw.strip()):
        return True
    return False
events = [ev for ev in events if not _is_blocked(ev)]

# Merge manually allowlisted events
_allowlist = cache.get("allowlist", [])
_existing_ids = set()
for ev in events:
    parts = ev.get("url", "").rstrip("/").split("/")
    _existing_ids.add(next((p for p in reversed(parts) if p.isdigit()), ""))
for _aid in _allowlist:
    if str(_aid) not in _existing_ids:
        _info = fetch_ra_event_info(_aid)
        if _info:
            events.append(_info)
events.sort(key=lambda x: (x.get("date_year") or now.year, x["date_sort"]))

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

# ── Admin panel — bottom of page ─────────────────────────────────────────────
if not st.session_state.admin:
    with st.expander(" ", expanded=False):
        pw = st.text_input("Password", type="password", key="admin_pw", label_visibility="collapsed")
        if st.button("Login", key="admin_login", use_container_width=True):
            if hashlib.sha256((ADMIN_PASSWORD_SALT + pw).encode()).hexdigest() == ADMIN_PASSWORD_HASH:
                st.session_state.admin = True
                st.rerun()
            else:
                st.error("Wrong password")
else:
    with st.expander("⚙️ Admin", expanded=False):
        if st.button("🚪 Logout", use_container_width=True):
            st.session_state.admin = False
            st.rerun()

        st.markdown("---")
        _backend = st.radio("Backend", ["SerpAPI (Google)", "Anthropic API (Claude + web search)"], index=0)
        if "Anthropic" in _backend:
            _api_key = st.text_input("Anthropic API Key", value=cache.get("api_key", ""), type="password", placeholder="sk-ant-...")
            _serpapi_key = None
        else:
            _api_key = None
            _serpapi_key = st.text_input("SerpAPI Key", value=cache.get("serpapi_key", ""), type="password", placeholder="your serpapi key")

        if _api_key and _api_key != cache.get("api_key"):
            cache["api_key"] = _api_key; cache["backend"] = _backend; save_cache(cache)
        if _serpapi_key and _serpapi_key != cache.get("serpapi_key"):
            cache["serpapi_key"] = _serpapi_key; cache["backend"] = _backend; save_cache(cache)

        backend = _backend
        api_key = _api_key or ""
        serpapi_key = _serpapi_key or ""

        st.markdown("---")
        st.markdown("**🕐 Berlin:** `{}`".format(now.strftime("%H:%M")))
        _m1, _m2, _m3, _y1, _y2, _y3 = get_search_months(now)
        st.markdown("**🔍 Searching:** {} {} · {} {} · {} {}".format(_m1, _y1, _m2, _y2, _m3, _y3))

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

        st.markdown("---")
        st.markdown("**🚫 Blocklists**")

        with st.expander("🔢 Block by RA Event ID ({} hardcoded + {} custom)".format(
            len(BLOCKED_EVENT_IDS), len(cache.get("blocklist", []))
        )):
            st.caption("**Hardcoded IDs:**")
            for eid in sorted(BLOCKED_EVENT_IDS):
                st.code(eid, language=None)
            custom_ids = list(cache.get("blocklist", []))
            id_text = st.text_area("Custom blocked IDs", value="\n".join(custom_ids),
                                   height=80, key="id_blocklist_input", label_visibility="collapsed")
            if st.button("💾 Save ID Blocklist"):
                new_bl = [x.strip() for x in id_text.splitlines() if x.strip().isdigit()]
                cache["blocklist"] = new_bl; save_cache(cache)
                st.success("Saved {} custom IDs".format(len(new_bl)))

        with st.expander("🔤 Block by Event Name ({} hardcoded + {} custom)".format(
            len(BLOCKED_NAME_KEYWORDS), len(cache.get("name_blocklist", []))
        )):
            custom_names = list(cache.get("name_blocklist", []))
            name_text = st.text_area("Custom blocked names", value="\n".join(custom_names),
                                     height=100, key="name_blocklist_input", label_visibility="collapsed")
            if st.button("💾 Save Name Blocklist"):
                new_nbl = [x.strip() for x in name_text.splitlines() if x.strip()]
                cache["name_blocklist"] = new_nbl; save_cache(cache)
                st.success("Saved {} custom name keywords".format(len(new_nbl)))

        with st.expander("✅ Allowlist ({} manual events)".format(len(cache.get("allowlist", [])))):
            st.caption("Add RA event IDs to always show, even if not fetched:")
            allow_ids = list(cache.get("allowlist", []))
            allow_text = st.text_area("Allowlist IDs", value="\n".join(str(x) for x in allow_ids),
                                      height=80, key="allowlist_input", label_visibility="collapsed")
            if st.button("💾 Save Allowlist"):
                new_al = [x.strip() for x in allow_text.splitlines() if x.strip().isdigit()]
                cache["allowlist"] = new_al; save_cache(cache)
                st.success("Saved {} IDs".format(len(new_al)))
                st.rerun()

        if st.button("🔍 Fetch Now", use_container_width=True):
            st.session_state.fetch_requested = True
            st.rerun()
