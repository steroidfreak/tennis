"""
Dafabet Tennis Duplicate Match Detector
========================================
Monitors https://sports.dafabet.com/en/live/sport/239-TENN every
CHECK_INTERVAL seconds.

Detects when two live match listings are likely the SAME real match
listed twice with slightly different player name formats, e.g.:

  Match A: "Butvilas, Edas vs Imamura, Masamichi"
  Match B: "Butvilas, E vs Imamura, M"

Name formats observed on the site:
  • "Lastname, Firstname"              (full name,   singles)
  • "Lastname, F"                      (initial only, singles)
  • "Lastname, F M"                    (initial + middle initial, comma)
  • "Lastname F"                       (no comma, singles – e.g. "Shimizu Y")
  • "Lastname F M"                     (no comma, first + middle initial – e.g. "Romios M C")
  • "Lastname Firstname M"             (no comma, full first + middle initial)
  • "Lastname1, F1/Lastname2, F2"      (doubles pair, slash no spaces)
  • "Lastname1 F1 / Lastname2 F2"      (doubles pair, slash with spaces)

The matching model compares every live match pair and alerts via Telegram
when the similarity score exceeds SIMILARITY_THRESHOLD.

Quick-start
-----------
1. cp .env.example .env   # fill in credentials
2. pip install -r requirements.txt
3. playwright install chromium
4. python monitor.py

Required .env keys:
  TELEGRAM_BOT_TOKEN   – Telegram bot token
  TELEGRAM_CHAT_ID     – Telegram chat/group ID

Optional .env keys:
  CHECK_INTERVAL       – seconds between polls (default: 60)
  HEARTBEAT_INTERVAL   – (removed) heartbeat is now sent daily at 07:00 UTC
  SIMILARITY_THRESHOLD – rule-based duplicate threshold (default: 0.75)
  MIN_SIDE_SCORE       – rule-based per-side floor (default: 0.60)
  HEADLESS             – run browser headless (default: true)
  AI_ANALYSIS          – enable LLM analysis layer (default: true)
  MINIMAX_API_KEY      – required when AI_ANALYSIS=true (MiniMax-M2.7)
"""

import asyncio
import json
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

import httpx
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page

# ── Load secrets from .env (never commit .env to git) ─────────────
load_dotenv()

# ══════════════════════════════════════════════════════════════════
#  CONFIG  ← sensitive values live in .env; tune the rest here
# ══════════════════════════════════════════════════════════════════

# Each entry is a (bot_token, chat_id) pair.
# Primary: TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
# Optional second: TELEGRAM_BOT_TOKEN_2 + TELEGRAM_CHAT_ID_2
def _build_telegram_recipients() -> list[tuple[str, str]]:
    pairs = [(os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"])]
    tok2 = os.getenv("TELEGRAM_BOT_TOKEN_2", "").strip()
    cid2 = os.getenv("TELEGRAM_CHAT_ID_2", "").strip()
    if tok2 and cid2:
        pairs.append((tok2, cid2))
    return pairs

TELEGRAM_RECIPIENTS: list[tuple[str, str]] = _build_telegram_recipients()

# Seconds between each poll of the tennis listing page (default 1 minute)
CHECK_INTERVAL: int       = int(os.getenv("CHECK_INTERVAL", "120"))

# Heartbeat is sent daily at 07:00 UTC (no longer configurable via env)

# Similarity score (0.0–1.0) above which a pair is flagged as a likely duplicate.
# Lower = more sensitive (more alerts); higher = stricter.
SIMILARITY_THRESHOLD: float = float(os.getenv("SIMILARITY_THRESHOLD", "0.75"))

# Minimum per-side score – both home AND away must exceed this floor
MIN_SIDE_SCORE: float       = float(os.getenv("MIN_SIDE_SCORE", "0.60"))

# Run browser without a visible window.
# MUST be True on a headless VPS (no display); False for local debugging.
HEADLESS: bool = os.getenv("HEADLESS", "true").lower() in ("1", "true", "yes")

# Enable AI analysis layer (default: true)
AI_ANALYSIS: bool = os.getenv("AI_ANALYSIS", "true").lower() in ("1", "true", "yes")

# MiniMax API key – required when AI_ANALYSIS=true
MINIMAX_API_KEY: str = os.getenv("MINIMAX_API_KEY", "")

# Tennis live page URL
TENNIS_URL: str = "https://sports.dafabet.com/en/live/sport/239-TENN"

# File used to persist alerted pairs across restarts
PAIRS_FILE: Path = Path(".alerted_pairs.json")

# Directory for anomaly investigation reports
ANOMALY_DIR: Path = Path("anomaly_reports")

# ══════════════════════════════════════════════════════════════════


# ── Name parsing & similarity ──────────────────────────────────────

def _ascii_lower(s: str) -> str:
    """Strip accents and lowercase."""
    nfkd = unicodedata.normalize("NFKD", s)
    return nfkd.encode("ascii", "ignore").decode().lower()


def parse_player(raw: str) -> dict:
    """
    Parse a single player name into structured components.

    Returns dict with keys: surname, first, initial, raw_lower

    Handles all real formats observed on Dafabet tennis pages:
      "Butvilas, Edas"        → surname="butvilas", first="edas",  initial="e"
      "Gorgodze, E"           → surname="gorgodze", first="",      initial="e"
      "Alcala Gurri, M"       → surname="alcala gurri", first="",  initial="m"
      "Mintegi del Olmo, A"   → surname="mintegi del olmo", initial="a"
      "Shimizu Y"             → surname="shimizu", initial="y"
      "Romios M C"            → surname="romios",  initial="m"  (middle initial C ignored)
      "Smith John C"          → surname="smith",   first="john", initial="j"
    """
    s = re.sub(r"\s+", " ", raw.strip())
    raw_lower = _ascii_lower(s)
    first = ""

    if "," in s:
        # "Surname[s], Firstname-or-Initial [MiddleInitial…]"
        # Everything before the first comma is the (possibly compound) surname.
        # Only the first token after the comma matters; trailing middle initials ignored.
        surname_part, rest = s.split(",", 1)
        surname = _ascii_lower(surname_part.strip())
        tokens = rest.strip().split()
        if tokens:
            tok0 = tokens[0].rstrip(".")
            if len(tok0) == 1:
                initial = tok0.lower()
            else:
                initial = tok0[0].lower()
                first   = _ascii_lower(tok0)
        else:
            initial = ""
    else:
        # No-comma formats:
        #   "Surname Initial"         – 2 tokens, last is 1 char  → "Shimizu Y"
        #   "Surname M C"             – 3+ tokens, all trailing are initials → "Romios M C"
        #   "Surname Firstname C"     – 3+ tokens, second is a full name → "Smith John C"
        #   "Surname Firstname"       – 2 tokens, last is multi-char
        tokens = s.split()
        if len(tokens) >= 2:
            last_tok = tokens[-1].rstrip(".")
            if len(last_tok) == 1:
                # Trailing token is an initial.
                # Surname is ALWAYS just the first token (never absorb middle initials).
                surname = _ascii_lower(tokens[0])
                if len(tokens) == 2:
                    # "Shimizu Y" – simple case
                    initial = last_tok.lower()
                else:
                    # 3+ tokens: "Romios M C" or "Smith John C"
                    second = tokens[1].rstrip(".")
                    if len(second) == 1:
                        # All post-surname tokens are initials; use the first one
                        initial = second.lower()
                    else:
                        # Second token is a full first name, last token is middle initial
                        initial = second[0].lower()
                        first   = _ascii_lower(second)
            else:
                # "Surname Firstname" – last token is a full first name
                initial = last_tok[0].lower()
                first   = _ascii_lower(last_tok)
                surname = _ascii_lower(tokens[0])
        else:
            surname = raw_lower
            initial = ""

    return {"surname": surname, "first": first, "initial": initial, "raw_lower": raw_lower}


def _fuzzy(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def player_similarity(name_a: str, name_b: str) -> float:
    """
    Return a 0.0–1.0 similarity score between two player name strings.

    Scoring guide:
      1.00 – identical strings
      0.92 – same surname + same first initial (one may be abbreviated)
      0.85 – same surname + both full first names that look like transliterations
      0.70 – same surname, no initials to compare
      0.15 – same surname but DIFFERENT first initials (different person)
      <0.70– surname mismatch → overall fuzzy fallback
    """
    if not name_a or not name_b:
        return 0.0

    a = parse_player(name_a)
    b = parse_player(name_b)

    if a["raw_lower"] == b["raw_lower"]:
        return 1.0

    surname_sim = _fuzzy(a["surname"], b["surname"])

    if surname_sim >= 0.85:
        ai, bi = a["initial"], b["initial"]
        af, bf = a["first"],   b["first"]

        if ai and bi:
            if ai != bi:
                # Confirmed different first initial → almost certainly different player
                return 0.15
            # Same initial
            if af and bf and af != bf:
                # Both have full first names that differ; allow slight fuzzy for transliteration
                first_sim = _fuzzy(af, bf)
                if first_sim >= 0.70:
                    return 0.85
                return 0.60
            # One or both abbreviated – can't confirm mismatch
            return 0.92

        # At least one side has no initial info
        return 0.70 * surname_sim

    # Surnames differ significantly – fall back to whole-string fuzzy
    return _fuzzy(a["raw_lower"], b["raw_lower"]) * 0.70


def split_doubles(name: str) -> list[str]:
    """Split a doubles entry like "Riera, Julia/Romero Gormaz, Leyre" into two players."""
    parts = re.split(r"\s*/\s*", name)
    return parts if len(parts) == 2 else [name]


def side_similarity(side_a: str, side_b: str) -> float:
    """Compare one side of a match (handles singles and doubles)."""
    pa = split_doubles(side_a)
    pb = split_doubles(side_b)

    if len(pa) == 1 and len(pb) == 1:
        return player_similarity(pa[0], pb[0])

    if len(pa) == 2 and len(pb) == 2:
        # In-order comparison
        s_ordered = (player_similarity(pa[0], pb[0]) + player_similarity(pa[1], pb[1])) / 2
        # Reverse partner order (rare but guard against it)
        s_reversed = (player_similarity(pa[0], pb[1]) + player_similarity(pa[1], pb[0])) / 2
        return max(s_ordered, s_reversed)

    # Mixed singles vs doubles → not the same match
    return 0.0


def match_similarity(entry_a: dict, entry_b: dict) -> tuple[float, str]:
    """
    Compare two match entries (each has 'home', 'away', 'url').
    Returns (overall_score, human-readable explanation).

    Checks both normal pairing (home↔home, away↔away)
    and reversed pairing (home↔away, away↔home).
    """
    # Normal pairing
    h_norm = side_similarity(entry_a["home"], entry_b["home"])
    a_norm = side_similarity(entry_a["away"], entry_b["away"])
    s_norm = (h_norm + a_norm) / 2

    # Reversed pairing (match listed with sides swapped)
    h_rev  = side_similarity(entry_a["home"], entry_b["away"])
    a_rev  = side_similarity(entry_a["away"], entry_b["home"])
    s_rev  = (h_rev + a_rev) / 2

    if s_norm >= s_rev:
        score = s_norm
        min_side = min(h_norm, a_norm)
        expl = (
            f"  Home: {entry_a['home']!r} ↔ {entry_b['home']!r}  [{h_norm:.2f}]\n"
            f"  Away: {entry_a['away']!r} ↔ {entry_b['away']!r}  [{a_norm:.2f}]"
        )
    else:
        score = s_rev
        min_side = min(h_rev, a_rev)
        expl = (
            f"  HomeA↔AwayB: {entry_a['home']!r} ↔ {entry_b['away']!r}  [{h_rev:.2f}]\n"
            f"  AwayA↔HomeB: {entry_a['away']!r} ↔ {entry_b['home']!r}  [{a_rev:.2f}]"
        )

    # Reject if either side scored below the floor (prevents one-sided matches)
    if min_side < MIN_SIDE_SCORE:
        score = min(score, MIN_SIDE_SCORE - 0.01)

    return score, expl


def detect_duplicates(entries: list[dict]) -> list[dict]:
    """
    Compare all n*(n-1)/2 pairs of live match entries.
    Return list of pairs that exceed SIMILARITY_THRESHOLD.
    """
    suspects = []
    for i in range(len(entries)):
        for j in range(i + 1, len(entries)):
            a, b = entries[i], entries[j]
            score, expl = match_similarity(a, b)
            if score >= SIMILARITY_THRESHOLD:
                suspects.append({
                    "score":       score,
                    "match_a":     a,
                    "match_b":     b,
                    "explanation": expl,
                    "pair_key":    frozenset([a["url"], b["url"]]),
                })
    return suspects


def confidence_label(score: float) -> str:
    if score >= 0.92:
        return "Very high"
    if score >= 0.82:
        return "High"
    return "Moderate"


# ── Persistence ────────────────────────────────────────────────────

def load_alerted_pairs() -> set[frozenset]:
    """Load previously alerted URL pairs from disk (survives restarts)."""
    if PAIRS_FILE.exists():
        try:
            data = json.loads(PAIRS_FILE.read_text(encoding="utf-8"))
            return {frozenset(p) for p in data}
        except Exception as exc:
            print(f"[warn] Could not load alerted pairs: {exc}")
    return set()


def save_alerted_pairs(pairs: set[frozenset]) -> None:
    """Persist alerted URL pairs to disk."""
    try:
        PAIRS_FILE.write_text(
            json.dumps([sorted(p) for p in pairs], indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        print(f"[warn] Could not save alerted pairs: {exc}")


def _build_ai_prompt(entries: list[dict]) -> str:
    """Shared prompt for all LLM providers."""
    match_list = "\n".join(
        f"{i + 1}. Home: {e['home']} | Away: {e['away']} | Section: {e.get('section') or 'unknown'}"
        for i, e in enumerate(entries)
    )
    return (
        "You are a tennis match integrity monitor for a live sports betting site.\n"
        "Below is the complete list of currently live tennis matches.\n\n"
        "Find these issues:\n"
        "1. DUPLICATE — same real match listed twice with different name formats.\n"
        "   Example: 'Butvilas, Edas' vs 'Butvilas, E' are the same person.\n"
        "   Also flag reversed listings: 'Samrej, K vs Xiao, L' and 'Xiao Lexue vs Samrej, K'\n"
        "   are the SAME match with sides swapped.\n"
        "2. PLAYER_CONFLICT — same real player appearing in two DIFFERENT matches simultaneously.\n\n"
        "Hard rules:\n"
        "- Different sections/tournaments → never a duplicate.\n"
        "- Singles vs doubles (slash in name) → never a duplicate.\n"
        "- Surname alone is NOT enough — need first initial or full name to confirm.\n"
        "- Name format differences (comma/no comma, abbreviated/full) are expected — do not flag these alone.\n"
        "- Only flag issues you are confident about.\n\n"
        "Output ONLY valid JSON, no markdown:\n"
        '{"issues": [{"type": "DUPLICATE" or "PLAYER_CONFLICT", '
        '"indices": [i, j], "confidence": "high" or "medium" or "low", "reason": "..."}]}\n\n'
        "Indices are 1-based. If no issues found, output {\"issues\": []}.\n\n"
        f"LIVE MATCHES ({len(entries)} total):\n{match_list}"
    )


def _parse_ai_response(text: str, entries: list[dict]) -> list[dict]:
    """Extract and validate issues from raw LLM JSON response (shared by all providers)."""
    # Strip <think>...</think> reasoning blocks (MiniMax-M2.7 emits these)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    start = text.find("{")
    end   = text.rfind("}") + 1
    if start == -1 or end <= start:
        print(f"[AI] No JSON in response: {text[:200]}")
        return []
    try:
        data = json.loads(text[start:end])
    except json.JSONDecodeError as exc:
        print(f"[AI] JSON parse error: {exc}  raw: {text[start:start+200]}")
        return []

    issues: list[dict] = []
    for item in data.get("issues", []):
        idxs = item.get("indices", [])
        if len(idxs) < 2:
            continue
        i, j = idxs[0] - 1, idxs[1] - 1   # 1-based → 0-based
        if not (0 <= i < len(entries) and 0 <= j < len(entries) and i != j):
            continue
        issues.append({
            "type":          item.get("type", "DUPLICATE"),
            "match_indices": [i, j],
            "explanation":   item.get("reason", ""),
            "confidence":    item.get("confidence", "medium"),
        })
    return issues


async def _call_minimax(prompt: str) -> str:
    """Call MiniMax-M2.7 via its OpenAI-compatible API and return the text response."""
    headers = {
        "Authorization": f"Bearer {MINIMAX_API_KEY}",
        "Content-Type":  "application/json",
    }
    payload = {
        "model":      "MiniMax-M2.7",
        "messages":   [{"role": "user", "content": prompt}],
        "max_tokens": 1024,
    }
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            "https://api.minimax.io/v1/chat/completions",
            headers=headers,
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


async def ai_analyze_matches(entries: list[dict]) -> list[dict]:
    """
    Send live match entries to MiniMax-M2.7 for anomaly detection.

    Detects:
      DUPLICATE       – same real match listed twice (incl. sides swapped / name formats)
      PLAYER_CONFLICT – same player in two different live matches simultaneously

    Returns list of issue dicts with 0-based match_indices.
    """
    if len(entries) < 2:
        return []

    prompt = _build_ai_prompt(entries)

    try:
        text = await _call_minimax(prompt)
    except Exception as exc:
        print(f"[AI] MiniMax call error: {exc}")
        return []

    return _parse_ai_response(text, entries)


# ── Dafabet scraping ───────────────────────────────────────────────

async def expand_all_sections(page: Page) -> int:
    """
    Click every collapsed league/group header so all hidden matches become visible.

    Confirmed structure (2026-02-27 analysis):
      Collapsed header: div[data-state="closed"][class*="bg-th-card-container"]
      Expanded header:  div[data-state="open"][class*="bg-th-card-container"]

    Returns the number of sections that were expanded.
    """
    # Grab all collapsed headers as element handles (must be done before clicking,
    # since clicking one can reflow the DOM)
    closed = await page.query_selector_all(
        'div[data-state="closed"][class*="bg-th-card-container"]'
    )
    if not closed:
        return 0

    print(f"  [*] Expanding {len(closed)} collapsed section(s)…")
    for header in closed:
        try:
            await header.scroll_into_view_if_needed()
            await header.click()
            await page.wait_for_timeout(250)   # let each section animate open
        except Exception as exc:
            print(f"  [warn] Could not expand section: {exc}")

    # Give the last sections a moment to fully render their match cards
    await page.wait_for_timeout(800)
    return len(closed)


async def extract_matches(page: Page, url: str) -> list[dict]:
    """
    Reload the tennis listing page, expand ALL collapsed sections, then return
    every live match entry: [{"url": ..., "home": ..., "away": ...}, ...]

    Confirmed DOM structure:
      - Collapsed section headers: div[data-state="closed"][class*="bg-th-card-container"]
      - Match links:  a[href] matching /en/live/<id>-...-vs-...
      - Player names: first two div.truncate[class*="text-th-primary-text"] inside
                      the match card (link's parent element)
    """
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=25_000)
    except Exception as exc:
        print(f"[warn] page load: {exc}")
    await page.wait_for_timeout(4_000)

    # ── Expand every collapsed league/group section ───────────────
    n_expanded = await expand_all_sections(page)
    if n_expanded:
        print(f"  [*] {n_expanded} section(s) expanded.")

    entries: list[dict] = await page.evaluate(
        """
        () => {
            const matchRe = /\\/en\\/live\\/\\d+-.+-vs-/;
            const cls = e => (e.getAttribute('class') || '');
            const seen = new Set();
            const results = [];

            for (const link of document.querySelectorAll('a[href]')) {
                const href = link.href.split('?')[0];
                if (!matchRe.test(new URL(link.href).pathname)) continue;
                if (seen.has(href)) continue;
                seen.add(href);

                // Find section/tournament name (best-effort, graceful on miss)
                let section = "";
                let secEl = link.parentElement;
                for (let sd = 0; sd < 20 && secEl; sd++) {
                    const sc = secEl.getAttribute('class') || '';
                    if (sc.includes('bg-th-card-container') && secEl.hasAttribute('data-state')) {
                        for (const ch of secEl.children) {
                            const t = ch.innerText ? ch.innerText.trim().split(String.fromCharCode(10))[0] : '';
                            if (t.length > 2 && t.length < 120 && !t.includes(' vs ') && !t.includes('/')) {
                                section = t;
                                break;
                            }
                        }
                        break;
                    }
                    secEl = secEl.parentElement;
                }

                // Walk up from the link to find the card container
                let container = link.parentElement;
                let found = false;
                for (let depth = 0; depth < 8 && container; depth++) {
                    const nameDivs = [...container.querySelectorAll('div')].filter(d => {
                        const c = cls(d);
                        return c.includes('truncate') && c.includes('text-th-primary-text');
                    });
                    if (nameDivs.length >= 2) {
                        // Detect "Not Started" status visible in the card on the listing page
                        const cardText = (container.innerText || '').toLowerCase();
                        const notStarted = cardText.includes('not started');
                        results.push({
                            url:         href,
                            home:        nameDivs[0].innerText.trim(),
                            away:        nameDivs[1].innerText.trim(),
                            section:     section,
                            not_started: notStarted,
                        });
                        found = true;
                        break;
                    }
                    container = container.parentElement;
                }
                if (!found) {
                    // Fallback: parse names from URL slug
                    const slug = new URL(link.href).pathname.replace('/en/live/', '');
                    const vsIdx = slug.indexOf('-vs-');
                    if (vsIdx !== -1) {
                        const numEnd = slug.indexOf('-');
                        const homePart = slug.slice(numEnd + 1, vsIdx).replace(/-/g, ' ');
                        const awayPart = slug.slice(vsIdx + 4).replace(/-/g, ' ');
                        results.push({ url: href, home: homePart, away: awayPart, section: section, not_started: false });
                    }
                }
            }
            return results;
        }
        """
    )
    return entries


# ── Telegram helpers ───────────────────────────────────────────────

async def send_telegram(text: str) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        for bot_token, chat_id in TELEGRAM_RECIPIENTS:
            api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
            try:
                resp = await client.post(api_url, json=payload)
                if not resp.is_success:
                    print(f"[Telegram] {chat_id}: {resp.status_code}: {resp.text[:200]}")
            except Exception as exc:
                print(f"[Telegram] {chat_id}: error: {exc}")


def _seconds_until_next_7am_utc() -> float:
    """Return seconds until the next 07:00 UTC."""
    now = datetime.now(timezone.utc)
    target = now.replace(hour=7, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def heartbeat_loop(
    started_at:      datetime,
    current_matches: list[dict],
    pending_reports: list[dict],
) -> None:
    """
    Send a Telegram 'still alive' message every day at 07:00 UTC.
    After the heartbeat, flush any anomaly reports accumulated since last heartbeat.
    Runs as a background asyncio task alongside the main polling loop.
    """
    wait = _seconds_until_next_7am_utc()
    print(f"[heartbeat] Next heartbeat in {wait / 3600:.1f}h (07:00 UTC).")
    await asyncio.sleep(wait)

    while True:
        uptime_secs = int((datetime.now(timezone.utc) - started_at).total_seconds())
        hours, rem  = divmod(uptime_secs, 3600)
        minutes     = rem // 60
        now_str     = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        if current_matches:
            match_lines = "\n".join(
                f"  {i + 1}. {e['home']} vs {e['away']}"
                for i, e in enumerate(current_matches)
            )
            match_section = f"\n\n🎾 <b>Live matches ({len(current_matches)}):</b>\n{match_lines}"
        else:
            match_section = "\n\n🎾 No live matches right now."

        # Count reports accumulated since last heartbeat for the summary line
        n_reports = len(pending_reports)
        report_summary = (
            f"\n\n📋 <b>Anomalies since last heartbeat:</b> {n_reports}"
            if n_reports else "\n\n📋 No anomalies since last heartbeat."
        )

        msg = (
            f"💓 <b>Monitor heartbeat</b>\n"
            f"Uptime: <b>{hours}h {minutes}m</b>  |  {now_str}"
            f"{match_section}"
            f"{report_summary}"
        )
        print(f"[heartbeat] Sending alive message (uptime {hours}h {minutes}m), "
              f"{len(current_matches)} live match(es), {n_reports} report(s)")
        await send_telegram(msg)

        # ── Flush pending anomaly reports ──────────────────────────────
        if pending_reports:
            reports_to_send = pending_reports.copy()
            pending_reports.clear()
            for r in reports_to_send:
                decision_emoji = "🔴" if r["decision"] == "ALERTED" else "🟡"
                decision_label = (
                    "ALERTED — sent to you in real-time"
                    if r["decision"] == "ALERTED"
                    else "SKIPPED — one match live, other not started (different dates)"
                )
                report_msg = (
                    f"{decision_emoji} <b>Anomaly report [{r['type']}]</b>\n"
                    f"Decision : <b>{decision_label}</b>\n"
                    f"Time     : {r['timestamp']}\n\n"
                    f"<b>Match A:</b>  {r['match_a_home']}  vs  {r['match_a_away']}\n"
                    f"  Status: {r['status_a']}  |  Score: {r['score_a'] or '—'}  |  "
                    f"Start: {r['start_a'] or '—'}\n\n"
                    f"<b>Match B:</b>  {r['match_b_home']}  vs  {r['match_b_away']}\n"
                    f"  Status: {r['status_b']}  |  Score: {r['score_b'] or '—'}  |  "
                    f"Start: {r['start_b'] or '—'}\n\n"
                    f"<b>Reason:</b> {r['explanation'][:300]}\n\n"
                    f"<b>Full report:</b> <code>{r['file']}</code>"
                )
                await send_telegram(report_msg)
                print(f"[heartbeat] Sent report: {r['file']}")

        # Sleep until next 07:00 UTC (accounts for drift)
        wait = _seconds_until_next_7am_utc()
        print(f"[heartbeat] Next heartbeat in {wait / 3600:.1f}h.")
        await asyncio.sleep(wait)


# ── Anomaly investigation ──────────────────────────────────────────

async def _extract_match_page_info(page, url: str) -> dict:
    """
    Open a match URL in the given page and extract key status elements.
    Returns dict: {url, status, start_time, score, raw_texts}
    """
    info = {"url": url, "status": "unknown", "start_time": "", "score": "", "raw_texts": []}
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=25_000)
        await page.wait_for_timeout(3_000)

        raw_texts = await page.evaluate(
            """
            () => {
                // Collect all meaningful short text nodes on the page
                const results = [];
                const walk = (el, depth) => {
                    if (depth > 8) return;
                    for (const child of el.children) {
                        const tag = child.tagName;
                        if (['SCRIPT', 'STYLE', 'NOSCRIPT'].includes(tag)) continue;
                        const t = (child.innerText || '').trim().split('\\n')[0].trim();
                        if (t.length > 0 && t.length < 200) results.push(t);
                        walk(child, depth + 1);
                    }
                };
                walk(document.body, 0);
                // Deduplicate while preserving order
                const seen = new Set();
                return results.filter(t => { if (seen.has(t)) return false; seen.add(t); return true; });
            }
            """
        )
        info["raw_texts"] = raw_texts[:120]  # cap to avoid huge files

        # Heuristic: look for status keywords in visible text
        combined = " ".join(info["raw_texts"]).lower()

        if any(k in combined for k in ("not started", "upcoming", "scheduled", "pre-match")):
            info["status"] = "not_started"
        elif any(k in combined for k in ("live", "in play", "in-play", "playing", "set ")):
            info["status"] = "live"
        elif any(k in combined for k in ("finished", "ended", "completed", "final")):
            info["status"] = "finished"

        # Try to find a start-time or score string
        for t in raw_texts:
            tl = t.lower()
            if re.search(r"\d{2}:\d{2}", t) and any(w in tl for w in ("start", "begin", "scheduled", "utc", "gmt")):
                info["start_time"] = t
                break
        for t in raw_texts:
            if re.match(r"^\d+[-–]\d+$", t.strip()):
                info["score"] = t.strip()
                break

    except Exception as exc:
        print(f"  [warn] investigate {url}: {exc}")

    return info


def _save_anomaly_report(
    anomaly_type:    str,
    match_a:         dict,
    match_b:         dict,
    info_a:          dict,
    info_b:          dict,
    explanation:     str,
    decision:        str,
    pending_reports: list[dict],
) -> Path:
    """
    Save a detailed anomaly investigation report to anomaly_reports/ and return the path.
    Also appends a compact summary to pending_reports for the next heartbeat flush.
    decision: "ALERTED" | "SKIPPED_DIFFERENT_STATUS"
    """
    ANOMALY_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")

    # Build a short slug from the match URLs
    def _slug(url: str) -> str:
        part = url.rstrip("/").split("/")[-1]
        return re.sub(r"[^a-z0-9\-]", "", part.lower())[:40]

    slug = _slug(match_a["url"])
    fname = ANOMALY_DIR / f"{timestamp}_{slug}.txt"

    def _fmt_texts(texts: list[str]) -> str:
        return "\n    ".join(texts[:40]) if texts else "(none)"

    report = (
        f"ANOMALY INVESTIGATION REPORT\n"
        f"============================\n"
        f"Timestamp : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
        f"Type      : {anomaly_type}\n"
        f"Decision  : {decision}\n\n"
        f"MATCH A\n"
        f"-------\n"
        f"  Home    : {match_a['home']}\n"
        f"  Away    : {match_a['away']}\n"
        f"  Section : {match_a.get('section', '')}\n"
        f"  URL     : {match_a['url']}\n"
        f"  Status  : {info_a['status']}\n"
        f"  Score   : {info_a['score']}\n"
        f"  Start   : {info_a['start_time']}\n"
        f"  Page texts (first 40):\n"
        f"    {_fmt_texts(info_a['raw_texts'])}\n\n"
        f"MATCH B\n"
        f"-------\n"
        f"  Home    : {match_b['home']}\n"
        f"  Away    : {match_b['away']}\n"
        f"  Section : {match_b.get('section', '')}\n"
        f"  URL     : {match_b['url']}\n"
        f"  Status  : {info_b['status']}\n"
        f"  Score   : {info_b['score']}\n"
        f"  Start   : {info_b['start_time']}\n"
        f"  Page texts (first 40):\n"
        f"    {_fmt_texts(info_b['raw_texts'])}\n\n"
        f"ALGORITHM EXPLANATION\n"
        f"---------------------\n"
        f"{explanation}\n"
    )

    fname.write_text(report, encoding="utf-8")
    print(f"  [report] Saved anomaly report: {fname}")

    # Queue a compact summary for the next heartbeat flush
    pending_reports.append({
        "type":         anomaly_type,
        "decision":     decision,
        "timestamp":    datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "match_a_home": match_a["home"],
        "match_a_away": match_a["away"],
        "match_b_home": match_b["home"],
        "match_b_away": match_b["away"],
        "status_a":     info_a["status"],
        "status_b":     info_b["status"],
        "score_a":      info_a["score"],
        "score_b":      info_b["score"],
        "start_a":      info_a["start_time"],
        "start_b":      info_b["start_time"],
        "explanation":  explanation,
        "file":         str(fname),
    })

    return fname


async def investigate_and_decide(
    browser_context: object,
    match_a:         dict,
    match_b:         dict,
    anomaly_type:    str,
    explanation:     str,
    pending_reports: list[dict],
) -> tuple[bool, Path | None]:
    """
    Open both match URLs in separate tabs, extract status elements, and decide
    whether the flagged pair is a real anomaly or a false positive.

    Returns (should_alert: bool, report_path: Path | None).

    False positive rule:
      If one match is clearly 'live' and the other is clearly 'not_started',
      they are scheduled for different dates → skip alert.
    """
    print(f"  [investigate] Opening match tabs for anomaly check…")
    page_a = await browser_context.new_page()
    page_b = await browser_context.new_page()

    try:
        info_a, info_b = await asyncio.gather(
            _extract_match_page_info(page_a, match_a["url"]),
            _extract_match_page_info(page_b, match_b["url"]),
        )
    finally:
        await page_a.close()
        await page_b.close()

    print(f"  [investigate] A status={info_a['status']}  B status={info_b['status']}")

    statuses = {info_a["status"], info_b["status"]}
    is_false_positive = (
        "live" in statuses and "not_started" in statuses
    )

    decision = "SKIPPED_DIFFERENT_STATUS" if is_false_positive else "ALERTED"
    report_path = _save_anomaly_report(
        anomaly_type, match_a, match_b, info_a, info_b, explanation, decision, pending_reports
    )

    if is_false_positive:
        print(
            f"  [investigate] FALSE POSITIVE — one match live, other not started. "
            f"Skipping alert. Report: {report_path}"
        )
        return False, report_path

    return True, report_path


# ── Entry point ────────────────────────────────────────────────────

async def main() -> None:
    started_at       = datetime.now(timezone.utc)
    alerted_pairs    = load_alerted_pairs()
    pending_reports: list[dict] = []   # anomaly summaries queued for next heartbeat
    if alerted_pairs:
        print(f"[*] Loaded {len(alerted_pairs)} previously alerted pair(s) from disk.")

    # ── AI provider setup ──────────────────────────────────────────────
    ai_enabled: bool = False

    if AI_ANALYSIS:
        if MINIMAX_API_KEY:
            ai_enabled = True
            print("[*] AI analysis enabled – MiniMax-M2.7.")
        else:
            print("[!] AI_ANALYSIS=true but MINIMAX_API_KEY not set – AI disabled.")

    # ── Send startup ping BEFORE browser loads ────────────────────────
    ai_status = "MiniMax-M2.7 ✓" if ai_enabled else "rule-based only"
    await send_telegram(
        f"🟢 <b>Tennis duplicate monitor starting…</b>\n"
        f"AI analysis: <b>{ai_status}</b>\n"
        f"Polling every {CHECK_INTERVAL}s · Heartbeat daily at 07:00 UTC\n"
        f"Started at: {started_at.strftime('%Y-%m-%d %H:%M UTC')}"
    )

    # Shared list updated each scrape cycle; read by heartbeat_loop
    current_matches: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=HEADLESS,
            slow_mo=0 if HEADLESS else 60,   # no artificial delay needed on VPS
            args=["--no-sandbox", "--disable-dev-shm-usage"] if HEADLESS else [],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        page = await context.new_page()

        print(f"[*] Starting tennis duplicate detector. Polling every {CHECK_INTERVAL}s.")
        print(f"[*] Similarity threshold: {SIMILARITY_THRESHOLD}  |  Min per-side: {MIN_SIDE_SCORE}")
        print(f"[*] Heartbeat: daily at 07:00 UTC")
        print(f"[*] URL: {TENNIS_URL}\n")

        # ── Launch heartbeat as a background task ─────────────────────
        heartbeat_task = asyncio.create_task(
            heartbeat_loop(started_at, current_matches, pending_reports)
        )

        try:
            while True:
                entries = await extract_matches(page, TENNIS_URL)
                current_urls = {e["url"] for e in entries}

                # Keep heartbeat_loop up to date with latest match list
                current_matches.clear()
                current_matches.extend(entries)

                # ── Expire pairs where a match is no longer live ─────────
                expired = {pk for pk in alerted_pairs if not pk.issubset(current_urls)}
                if expired:
                    print(f"[*] {len(expired)} previously alerted pair(s) expired (match ended).")
                    alerted_pairs -= expired
                    save_alerted_pairs(alerted_pairs)

                if not entries:
                    print("[!] No live tennis matches found – will retry.")
                else:
                    # Separate matches by status detected on the listing page
                    live_entries       = [e for e in entries if not e.get("not_started")]
                    not_started_entries = [e for e in entries if e.get("not_started")]

                    print(f"\n[*] {len(entries)} match(es) on listing page "
                          f"({len(live_entries)} live, {len(not_started_entries)} not started):")
                    for e in live_entries:
                        print(f"    [LIVE]        {e['home']} vs {e['away']}")
                    for e in not_started_entries:
                        print(f"    [NOT STARTED] {e['home']} vs {e['away']}  ← excluded from checks")

                    suspects = detect_duplicates(live_entries)
                    new_suspects = [s for s in suspects if s["pair_key"] not in alerted_pairs]

                    if new_suspects:
                        print(f"\n[!] {len(new_suspects)} new duplicate pair(s) detected!")
                        for s in new_suspects:
                            a = s["match_a"]
                            b = s["match_b"]
                            pct   = int(s["score"] * 100)
                            label = confidence_label(s["score"])

                            print(
                                f"  [{label} – {pct}%]\n"
                                f"    A: {a['home']} vs {a['away']}\n"
                                f"    B: {b['home']} vs {b['away']}\n"
                                f"{s['explanation']}"
                            )

                            should_alert, report_path = await investigate_and_decide(
                                context, a, b, "DUPLICATE", s["explanation"], pending_reports
                            )
                            alerted_pairs.add(s["pair_key"])  # always suppress re-check

                            if not should_alert:
                                continue

                            report_note = f"\n\nReport saved: {report_path}" if report_path else ""
                            msg = (
                                f"🎾 <b>Possible duplicate tennis match!</b>\n"
                                f"Confidence: <b>{label} ({pct}%)</b>\n\n"
                                f"<b>Match A:</b>  {a['home']}  vs  {a['away']}\n"
                                f"<b>Match B:</b>  {b['home']}  vs  {b['away']}\n\n"
                                f"Name comparison:\n"
                                f"{s['explanation']}\n\n"
                                f"<a href='{a['url']}'>Open Match A</a>\n"
                                f"<a href='{b['url']}'>Open Match B</a>"
                                f"{report_note}"
                            )
                            await send_telegram(msg)
                        save_alerted_pairs(alerted_pairs)
                    else:
                        print("    No duplicates detected in this cycle.")

                    # ── AI analysis ───────────────────────────────────────────
                    if ai_enabled:
                        print(f"\n[AI] Running batch analysis (MiniMax-M2.7)…")
                        ai_issues = await ai_analyze_matches(live_entries)

                        new_ai = []
                        for issue in ai_issues:
                            idxs = issue.get("match_indices", [])
                            if len(idxs) < 2:
                                continue
                            i, j = idxs[0], idxs[1]
                            if i >= len(live_entries) or j >= len(live_entries) or i < 0 or j < 0:
                                continue
                            pair_key = frozenset([live_entries[i]["url"], live_entries[j]["url"]])
                            if pair_key not in alerted_pairs:
                                issue["pair_key"]  = pair_key
                                issue["match_a"]   = live_entries[i]
                                issue["match_b"]   = live_entries[j]
                                new_ai.append(issue)

                        if new_ai:
                            print(f"[AI] {len(new_ai)} new issue(s) detected!")
                            for issue in new_ai:
                                a    = issue["match_a"]
                                b    = issue["match_b"]
                                kind = issue["type"]
                                conf = issue["confidence"].capitalize()
                                expl = issue["explanation"]

                                print(
                                    f"  [{kind} – {conf}]\n"
                                    f"    A: {a['home']} vs {a['away']}\n"
                                    f"    B: {b['home']} vs {b['away']}\n"
                                    f"    {expl}"
                                )

                                should_alert, report_path = await investigate_and_decide(
                                    context, a, b, kind, expl, pending_reports
                                )
                                alerted_pairs.add(issue["pair_key"])  # always suppress re-check

                                if not should_alert:
                                    continue

                                if kind == "PLAYER_CONFLICT":
                                    emoji      = "⚠️"
                                    type_label = "Player conflict detected! (MiniMax-M2.7)"
                                else:  # DUPLICATE
                                    emoji      = "🎾"
                                    type_label = "Possible duplicate tennis match! (MiniMax-M2.7)"

                                report_note = f"\n\nReport saved: {report_path}" if report_path else ""
                                msg = (
                                    f"{emoji} <b>{type_label}</b>\n"
                                    f"Confidence: <b>{conf}</b>\n\n"
                                    f"<b>Match A:</b>  {a['home']}  vs  {a['away']}\n"
                                    f"<b>Match B:</b>  {b['home']}  vs  {b['away']}\n\n"
                                    f"<b>AI analysis:</b> {expl}\n\n"
                                    f"<a href='{a['url']}'>Open Match A</a>\n"
                                    f"<a href='{b['url']}'>Open Match B</a>"
                                    f"{report_note}"
                                )
                                await send_telegram(msg)
                            save_alerted_pairs(alerted_pairs)
                        else:
                            print("[AI] No new issues detected.")

                print(f"\n--- sleeping {CHECK_INTERVAL}s ---\n")
                await asyncio.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            print("\n[*] Stopped by user.")
        finally:
            heartbeat_task.cancel()
            await send_telegram(
                f"🔴 <b>Tennis duplicate monitor stopped</b>\n"
                f"Stopped at: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
            )
            await browser.close()
            print("[*] Browser closed.")


if __name__ == "__main__":
    asyncio.run(main())
