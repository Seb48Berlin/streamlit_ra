import streamlit as st
import requests
import re
import json
from datetime import datetime
import pytz

st.set_page_config(page_title="RA Berlin Free Events – Mar 2026", page_icon="🎵", layout="wide")

MONTH_ORDER = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Allowed fetch times (Berlin timezone): 11:00, 16:00, 21:00
ALLOWED_HOURS = {11, 16, 21}
# Cache lasts until next allowed slot — we use a long TTL and control manually
BERLIN_TZ = pytz.timezone("Europe/Berlin")

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


def get_berlin_now():
    return datetime.now(BERLIN_TZ)


def get_next_allowed_slot(now):
    """Return the next allowed fetch time as a datetime."""
    for h in sorted(ALLOWED_HOURS):
        candidate = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if candidate > now:
            return candidate
    # Next day's first slot
    tomorrow = (now + __import__('datetime').timedelta(days=1)).replace(
        hour=min(ALLOWED_HOURS), minute=0, second=0, microsecond=0
    )
    return tomorrow


def is_allowed_to_fetch(now):
    """True if current hour (Berlin) is one of the allowed slots."""
    return now.hour in ALLOWED_HOURS


def minutes_until_next(now):
    nxt = get_next_allowed_slot(now)
    delta = nxt - now
    total_mins = int(delta.total_seconds() // 60)
    hours, mins = divmod(total_mins, 60)
    return hours, mins, nxt


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
        sort_val = mon_num * 100 + day
        display = "{} {}".format(day, m.group(2).capitalize())
        return sort_val, display
    return 9999, sub


# Cache key = slot identifier (date + hour) so it only re-fetches at new slots
def slot_key(now):
    return now.strftime("%Y%m%d") + str(now.hour)


@st.cache_data(ttl=3600)
def fetch_via_anthropic(api_key, _cache_key):
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
        "messages": [
            {
                "role": "user",
                "content": 'site:ra.co/events "Berlin" "Free Entry" "Mar" "2026"'
            }
        ]
    }
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers,
            json=payload,
            timeout=60
        )
        resp.raise_for_status()
        data = resp.json()
        full_text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                full_text += block.get("text", "")
        clean = re.sub(r'```json|```', '', full_text).strip()
        m = re.search(r'\[.*\]', clean, re.DOTALL)
        if m:
            events = json.loads(m.group(0))
            return events, None
        return [], "No JSON array found in response"
    except requests.exceptions.HTTPError as e:
        return None, "API error: {}".format(str(e))
    except json.JSONDecodeError as e:
        return None, "JSON parse error: {}. Raw: {}".format(str(e), full_text[:300])
    except Exception as e:
        return None, str(e)


@st.cache_data(ttl=3600)
def fetch_via_serpapi(serpapi_key, _cache_key):
    params = {
        "engine": "google",
        "q": 'site:ra.co/events "Berlin" "Free Entry" "Mar" "2026"',
        "api_key": serpapi_key,
        "num": 20,
    }
    try:
        resp = requests.get("https://serpapi.com/search", params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("organic_results", [])
        events = []
        for r in results:
            href = r.get("link", "")
            if "ra.co/events" not in href:
                continue
            raw_title = r.get("title", "")
            raw_title = re.sub(r'\s*[⟋|]\s*RA\s*$', '', raw_title).strip()
            clean_title = remove_free_entry(raw_title)
            raw_sub = r.get("snippet", "")
            cleaned_sub = clean_subheading(raw_sub)
            sort_val, date_display = parse_date(cleaned_sub)
            m = re.search(r'\d{1,2}\s+[A-Za-z]{3}', cleaned_sub)
            subtitle = cleaned_sub[m.end():].strip().lstrip('·').strip() if m else cleaned_sub
            events.append({
                "title": clean_title,
                "url": href,
                "date_display": date_display,
                "date_sort": sort_val,
                "subtitle": subtitle,
            })
        events.sort(key=lambda x: x["date_sort"])
        return events, None
    except Exception as e:
        return None, str(e)


def normalize_events(raw_events):
    events = []
    for ev in raw_events:
        title = remove_free_entry(ev.get("title", ""))
        url = ev.get("url", ev.get("href", ""))
        date_display = ev.get("date_display", "")
        date_sort = ev.get("date_sort", 9999)
        subtitle = ev.get("subtitle", ev.get("subheading", ""))
        try:
            date_sort = int(date_sort)
        except (ValueError, TypeError):
            date_sort = 9999
        events.append({
            "title": title,
            "url": url,
            "date_display": date_display,
            "date_sort": date_sort,
            "subtitle": subtitle,
        })
    events.sort(key=lambda x: x["date_sort"])
    return events


# ── UI ────────────────────────────────────────────────────────────────────────

st.title("🎵 RA Berlin — Free Entry · Mar 2026")

now = get_berlin_now()

# Sidebar
with st.sidebar:
    st.header("⚙️ Configuration")

    backend = st.radio(
        "Backend",
        ["Anthropic API (Claude + web search)", "SerpAPI (Google)"],
        index=0
    )

    if "Anthropic" in backend:
        api_key = st.text_input(
            "Anthropic API Key",
            type="password",
            placeholder="sk-ant-...",
            help="Get yours at console.anthropic.com"
        )
        serpapi_key = None
    else:
        api_key = None
        serpapi_key = st.text_input(
            "SerpAPI Key",
            type="password",
            placeholder="your serpapi key",
            help="Free tier at serpapi.com (100 searches/month)"
        )

    st.markdown("---")

    # Show clock + next slot info
    st.markdown("**🕐 Berlin time:** `{}`".format(now.strftime("%H:%M")))

    allowed = is_allowed_to_fetch(now)
    if allowed:
        st.success("✅ Fetch window open ({}:00)".format(now.hour))
    else:
        h, m, nxt = minutes_until_next(now)
        st.warning(
            "⏳ Next fetch at **{}**\n\n({} h {} min from now)".format(
                nxt.strftime("%H:%M"), h, m
            )
        )

    st.markdown("---")

    # Usage tracker (stored in session state)
    if "fetch_count" not in st.session_state:
        st.session_state.fetch_count = 0
    if "fetch_log" not in st.session_state:
        st.session_state.fetch_log = []

    # Monthly budget: 3 fetches/day × 31 days = 93 (safely under 100)
    monthly_used = st.session_state.fetch_count
    monthly_budget = 93
    remaining = monthly_budget - monthly_used

    st.markdown("**📊 Search budget (SerpAPI)**")
    progress = min(monthly_used / monthly_budget, 1.0)
    st.progress(progress)
    st.caption("{} / {} used this session · {} remaining".format(
        monthly_used, monthly_budget, remaining
    ))

    if st.session_state.fetch_log:
        with st.expander("Fetch history"):
            for entry in reversed(st.session_state.fetch_log[-10:]):
                st.caption(entry)

    fetch_btn = st.button("🔍 Fetch Events", use_container_width=True, disabled=not allowed)

    if not allowed:
        st.caption("Button unlocks at next scheduled slot.")

# Main content
if not fetch_btn:
    h, m, nxt = minutes_until_next(now)
    if not allowed:
        st.info(
            "🕐 **Next scheduled fetch: {} Berlin time** ({} h {} min from now)\n\n"
            "Fetches are limited to **11:00, 16:00 and 21:00** to stay within "
            "the 100 searches/month budget.".format(nxt.strftime("%H:%M"), h, m)
        )
    else:
        st.info("Fetch window is open! Enter your API key in the sidebar and click **Fetch Events**.")
    st.stop()

if "Anthropic" in backend:
    if not api_key:
        st.error("Please enter your Anthropic API key.")
        st.stop()
    key = slot_key(now)
    with st.spinner("Searching via Claude + web search…"):
        raw_events, error = fetch_via_anthropic(api_key, key)
else:
    if not serpapi_key:
        st.error("Please enter your SerpAPI key.")
        st.stop()
    key = slot_key(now)
    with st.spinner("Searching via SerpAPI…"):
        raw_events, error = fetch_via_serpapi(serpapi_key, key)

# Track usage (only count if a real API call was made — cache hits don't increment)
if not error and raw_events is not None:
    log_entry = "[{}] Fetched via {}".format(
        now.strftime("%d %b %H:%M"),
        "Anthropic" if "Anthropic" in backend else "SerpAPI"
    )
    # Avoid double-counting cached results in same slot
    if not st.session_state.fetch_log or st.session_state.fetch_log[-1] != log_entry:
        st.session_state.fetch_count += 1
        st.session_state.fetch_log.append(log_entry)

if error:
    st.error("Error: {}".format(error))
    st.stop()

if not raw_events:
    st.warning("No events found. Try refreshing or check your API key.")
    st.stop()

events = normalize_events(raw_events)

st.success("Found **{}** event(s) — last fetched {}".format(
    len(events), now.strftime("%d %b %H:%M")
))
st.divider()

for ev in events:
    date_str = ev["date_display"]
    subtitle = ev["subtitle"]

    col1, col2 = st.columns([1, 6])
    with col1:
        if date_str:
            st.markdown(
                '<div style="background:#1a1a2e;color:#e0e0ff;border-radius:8px;'
                'padding:8px 4px;text-align:center;font-weight:700;font-size:1rem;">'
                '{}</div>'.format(date_str),
                unsafe_allow_html=True
            )
    with col2:
        title_html = (
            '<a href="{}" target="_blank" '
            'style="font-size:1.05rem;font-weight:600;text-decoration:none;color:#4f8ef7;">'
            '{}</a>'
        ).format(ev["url"], ev["title"])
        st.markdown(title_html, unsafe_allow_html=True)
        if subtitle:
            st.caption(subtitle)

    st.divider()
