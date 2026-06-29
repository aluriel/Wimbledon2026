#!/usr/bin/env python3
"""
Flask web sunucusu: Wimbledon 2026 Erkekler ve Kadınlar Tekler takip sayfası.
Çalıştır, ardından http://localhost:5000 adresini aç.
Önbellek: canlı maç varken 5dk, yoksa sonraki maç başlangıcı + 5dk.
"""

import json
import re
import unicodedata
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, Response, request, redirect
import requests

# Official draw order (draw positions 1..128). ESPN's API does not expose draw
# positions, so we use the published draw to order the bracket view correctly.
try:
    from draw_data import MENS_DRAW_2026, WOMENS_DRAW_2026
except ImportError:  # pragma: no cover - import path differs under Vercel
    try:
        from tools.draw_data import MENS_DRAW_2026, WOMENS_DRAW_2026
    except ImportError:
        MENS_DRAW_2026, WOMENS_DRAW_2026 = [], []

TRT = timezone(timedelta(hours=3), name="TRT")

ESPN_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/tennis/atp/scoreboard?limit=300"
)

SINGLES_GROUPINGS = {"Men's Singles", "Women's Singles"}

MAIN_DRAW_ROUNDS = {"Round 1", "Round 2", "Round 3", "Round 4", "Quarterfinal", "Semifinal", "Final"}

STATUS_MAP = {
    "STATUS_SCHEDULED":   "Planlandı",
    "STATUS_IN_PROGRESS": "Canlı",
    "STATUS_IN_PLAY":     "Canlı",
    "STATUS_FINAL":       "Bitti",
    "STATUS_FULL_TIME":   "Bitti",
    "STATUS_END_PERIOD":  "Bitti",
    "STATUS_SUSPENDED":   "Askıda",
    "STATUS_RETIRED":     "Çekildi",
    "STATUS_WALKOVER":    "WO",
    "STATUS_RAIN_DELAY":  "Yağmur Molası",
}

ROUND_TR = {
    "Round 1":      "1. Tur",
    "Round 2":      "2. Tur",
    "Round 3":      "3. Tur",
    "Round 4":      "4. Tur",
    "Quarterfinal": "Çeyrek Final",
    "Semifinal":    "Yarı Final",
    "Final":        "Final",
}

EVENT_LABEL = {
    "Men's Singles":   "Erkekler Tekler",
    "Women's Singles": "Kadınlar Tekler",
}

EVENT_KEY = {
    "Men's Singles":   "erkekler-tekler",
    "Women's Singles": "kadinlar-tekler",
}


# ---------------------------------------------------------------------------
# Draw-position lookup (for correct bracket ordering)
# ---------------------------------------------------------------------------

def _norm_name(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().upper()
    return re.sub(r"[^A-Z]", "", s)


def _build_draw_index(draw: list) -> tuple[dict, dict]:
    """Return (by_surname, by_tokens). Draw position is the 1-based list index."""
    by_sur: dict = {}   # surname -> list of (first_initial, pos)
    by_tok: dict = {}   # frozenset({first, last}) -> pos  (handles name-order swaps)
    for i, (first, last) in enumerate(draw):
        pos = i + 1
        by_sur.setdefault(_norm_name(last), []).append((_norm_name(first)[:1], pos))
        by_tok[frozenset((_norm_name(first), _norm_name(last)))] = pos
    return by_sur, by_tok


DRAW_INDEX = {
    "erkekler-tekler": _build_draw_index(MENS_DRAW_2026),
    "kadinlar-tekler": _build_draw_index(WOMENS_DRAW_2026),
}


def _draw_position(short_name: str, display_name: str, by_sur: dict, by_tok: dict):
    """Best-effort draw position (1..128) for an ESPN player; None if unknown/TBD."""
    # Exact token-set match first (handles e.g. "Wu Yibing" name-order swaps).
    toks = frozenset(_norm_name(t) for t in (display_name or "").split() if t)
    if toks in by_tok:
        return by_tok[toks]
    # Otherwise use surname + first initial from shortName ("T. Tirante", "J.M. Cerundolo").
    m = re.match(r"^((?:[A-Za-z]\.)+)\s+(.+)$", short_name or "")
    if m:
        sur, ini = _norm_name(m.group(2)), _norm_name(m.group(1))[:1]
    else:
        parts = (display_name or "").split()
        sur = _norm_name(" ".join(parts[1:]) or display_name)
        ini = _norm_name(parts[0])[:1] if parts else ""
    cands = by_sur.get(sur, [])
    if len(cands) == 1:
        return cands[0][1]
    for cand_ini, pos in cands:
        if cand_ini == ini:
            return pos
    return cands[0][1] if cands else None


_cache: dict = {"data": None, "refresh_at": None}

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _build_set_detail(p1_ls: list, p2_ls: list) -> str:
    """Builds '6-3 7-6(4) 3-2' style string from both players' linescores."""
    n = max(len(p1_ls), len(p2_ls))
    parts = []
    for i in range(n):
        ls1 = p1_ls[i] if i < len(p1_ls) else {}
        ls2 = p2_ls[i] if i < len(p2_ls) else {}
        s1 = int(float(ls1.get("value", 0)))
        s2 = int(float(ls2.get("value", 0)))
        tb = ""
        if "tiebreak" in ls1 and "tiebreak" in ls2:
            if ls1.get("winner"):
                tb = f"({ls2['tiebreak']})"
            elif ls2.get("winner"):
                tb = f"({ls1['tiebreak']})"
        parts.append(f"{s1}-{s2}{tb}")
    return " ".join(parts)


def _parse_competition(comp: dict, grouping_name: str) -> dict | None:
    rnd = comp.get("round", {}).get("displayName", "")
    if rnd not in MAIN_DRAW_ROUNDS:
        return None

    status_obj = comp.get("status", {})
    status_type = status_obj.get("type", {})
    state = status_type.get("state", "pre")
    status_name = status_type.get("name", "STATUS_SCHEDULED")

    utc_str = comp.get("date", "")
    utc_dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
    trt_dt = utc_dt.astimezone(TRT)

    court = comp.get("venue", {}).get("court", "")

    # Sort by order so p1=order1(home), p2=order2(away)
    raw = sorted(comp.get("competitors", []), key=lambda c: c.get("order", 99))
    p1_raw = raw[0] if raw else {}
    p2_raw = raw[1] if len(raw) > 1 else {}

    def _player(c: dict) -> dict:
        athlete = c.get("athlete", {})
        flag = athlete.get("flag", {})
        cr = c.get("curatedRank")
        return {
            "id":       str(c.get("id", "")),
            "name":     athlete.get("displayName", ""),
            "short":    athlete.get("shortName", athlete.get("displayName", "")),
            "flag":     flag.get("href", ""),
            "flag_alt": flag.get("alt", ""),
            "winner":   c.get("winner", False),
            "rank":     int(cr["current"]) if cr and cr.get("current") else None,
        }

    p1 = _player(p1_raw)
    p2 = _player(p2_raw)

    # Seed extraction from notes: "(23) Rafael Jodar (ESP) bt ..."
    note_text = comp.get("notes", [{}])[0].get("text", "") if comp.get("notes") else ""
    seed_m = re.match(r'^\((\d+)\)\s+([\w\s\-\.\']+?)\s+\([A-Z]{2,3}\)', note_text)
    seed_num = int(seed_m.group(1)) if seed_m else None
    seed_name = seed_m.group(2).strip() if seed_m else None

    def _seed_for(player_name: str) -> int | None:
        if seed_num and seed_name and seed_name.lower() in player_name.lower():
            return seed_num
        return None

    p1_ls = p1_raw.get("linescores", [])
    p2_ls = p2_raw.get("linescores", [])

    p1_sets = sum(1 for ls in p1_ls if ls.get("winner") is True) if state != "pre" else None
    p2_sets = sum(1 for ls in p2_ls if ls.get("winner") is True) if state != "pre" else None
    set_detail = _build_set_detail(p1_ls, p2_ls) if state != "pre" else ""

    live_clock = status_type.get("detail", "") if state == "in" else ""

    return {
        "match_id":    comp["id"],
        "event_label": EVENT_LABEL.get(grouping_name, grouping_name),
        "event_key":   EVENT_KEY.get(grouping_name, ""),
        "round_label": ROUND_TR.get(rnd, rnd),
        "round_raw":   rnd,
        "court":       court,
        "p1_id":       p1["id"],
        "p1_name":     p1["name"],
        "p1_short":    p1["short"],
        "p1_flag":     p1["flag"],
        "p1_flag_alt": p1["flag_alt"],
        "p1_seed":     _seed_for(p1["name"]) or p1["rank"],
        "p1_rank":     p1["rank"],
        "p1_sets":     p1_sets,
        "p1_winner":   p1["winner"],
        "p2_id":       p2["id"],
        "p2_name":     p2["name"],
        "p2_short":    p2["short"],
        "p2_flag":     p2["flag"],
        "p2_flag_alt": p2["flag_alt"],
        "p2_seed":     _seed_for(p2["name"]) or p2["rank"],
        "p2_rank":     p2["rank"],
        "p2_sets":     p2_sets,
        "p2_winner":   p2["winner"],
        "set_detail":  set_detail,
        "date_trt":    trt_dt.strftime("%Y-%m-%d"),
        "time_trt":    trt_dt.strftime("%H:%M"),
        "date_label":  trt_dt.strftime("%d %b"),
        "day_label":   trt_dt.strftime("%A"),
        "status":      STATUS_MAP.get(status_name, status_type.get("description", "")),
        "status_raw":  status_name,
        "state":       state,
        "live_clock":  live_clock,
        "venue":       comp.get("venue", {}).get("fullName", ""),
        "kickoff_utc": utc_dt.isoformat(),
    }


# ---------------------------------------------------------------------------
# Data fetching & caching
# ---------------------------------------------------------------------------

def _compute_next_refresh(matches: list[dict]) -> datetime:
    now = datetime.now(timezone.utc)
    if any(m["state"] == "in" for m in matches):
        return now + timedelta(minutes=5)
    upcoming = [m for m in matches if m["state"] == "pre"]
    if upcoming:
        earliest = min(upcoming, key=lambda m: m["kickoff_utc"])
        kickoff = datetime.fromisoformat(earliest["kickoff_utc"])
        return kickoff + timedelta(minutes=5)
    return now + timedelta(hours=24)


def get_matches() -> list[dict]:
    now = datetime.now(timezone.utc)
    if _cache["data"] is not None and _cache["refresh_at"] is not None:
        if now < _cache["refresh_at"]:
            kickoff_passed = any(
                m["state"] == "pre" and datetime.fromisoformat(m["kickoff_utc"]) <= now
                for m in _cache["data"]
            )
            if not kickoff_passed:
                return _cache["data"]

    try:
        resp = requests.get(ESPN_URL, timeout=20, headers={"User-Agent": "WimbledonTracker/1.0"})
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as e:
        print(f"[HATA] ESPN API: {e}")
        return _cache["data"] or []

    wimbledon = next(
        (e for e in data.get("events", []) if "wimbledon" in e.get("name", "").lower()),
        None,
    )
    if not wimbledon:
        print("[UYARI] ESPN yanıtında Wimbledon bulunamadı")
        return _cache["data"] or []

    matches = []
    for grp in wimbledon.get("groupings", []):
        grp_name = grp.get("grouping", {}).get("displayName", "")
        if grp_name not in SINGLES_GROUPINGS:
            continue
        for comp in grp.get("competitions", []):
            parsed = _parse_competition(comp, grp_name)
            if parsed:
                matches.append(parsed)

    matches.sort(key=lambda m: m["date_trt"] + m["time_trt"])

    _cache["data"] = matches
    _cache["refresh_at"] = _compute_next_refresh(matches)

    live_n = sum(1 for m in matches if m["state"] == "in")
    refresh_trt = _cache["refresh_at"].astimezone(TRT).strftime("%H:%M TRT")
    print(f"[INFO] {len(matches)} maç yüklendi ({live_n} canlı). Sonraki güncelleme: {refresh_trt}")
    return matches


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def _flag_img(url: str, alt: str, css_class: str = "player-flag") -> str:
    if url:
        return f'<img class="{css_class}" src="{url}" alt="{alt}" loading="lazy">'
    return f'<div class="{css_class}-ph"></div>'


def _score_html(m: dict) -> str:
    if m["p1_sets"] is None:
        return '<div class="score-box"><span class="score-display pending">&#8211;&nbsp;vs&nbsp;&#8211;</span></div>'
    detail_html = f'<div class="set-detail">{m["set_detail"]}</div>' if m["set_detail"] else ""
    return (
        f'<div class="score-box">'
        f'<span class="score-display">{m["p1_sets"]} &#8211; {m["p2_sets"]}</span>'
        f'{detail_html}'
        f'</div>'
    )


def _player_html(m: dict, side: str) -> str:
    name = m[f"{side}_name"]
    short = m[f"{side}_short"]
    flag = m[f"{side}_flag"]
    flag_alt = m[f"{side}_flag_alt"]
    is_winner = m[f"{side}_winner"] and m["state"] == "post"
    winner_class = " winner" if is_winner else ""
    flag_html = _flag_img(flag, flag_alt)

    if side == "p1":
        return (
            f'<div class="player p1{winner_class}">'
            f'<span class="player-name">{name}</span>'
            f'<span class="player-short">{short}</span>'
            f'{flag_html}</div>'
        )
    return (
        f'<div class="player p2{winner_class}">'
        f'{flag_html}'
        f'<span class="player-name">{name}</span>'
        f'<span class="player-short">{short}</span>'
        f'</div>'
    )


def _badge(state: str, status: str, live_clock: str) -> str:
    if state == "in":
        clock = f'<div class="live-clock">{live_clock}</div>' if live_clock else ""
        return f'<span class="status-badge badge-live">&#128308; Canlı</span>{clock}'
    if state == "post":
        if "Çekil" in status or status == "WO":
            return f'<span class="status-badge badge-retired">{status}</span>'
        return '<span class="status-badge badge-finished">Bitti</span>'
    if "Askıda" in status or "Yağmur" in status:
        return f'<span class="status-badge badge-suspended">{status}</span>'
    return '<span class="status-badge badge-scheduled">Planlandı</span>'


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Wimbledon 2026</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background: #0a120a; color: #e8eaf6; min-height: 100vh; }

  /* Header */
  .header {
    background: linear-gradient(135deg, #0d2b0d 0%, #1a4d2e 50%, #2d1a42 100%);
    padding: 20px 24px 16px;
    display: flex; align-items: center; justify-content: space-between;
    flex-wrap: wrap; gap: 12px;
    border-bottom: 3px solid #7cfc00;
  }
  .header-title { display: flex; align-items: center; gap: 14px; }
  .header-title h1 { font-size: 1.5rem; font-weight: 700; color: #fff; }
  .header-title .year { color: #7cfc00; }
  .trophy { font-size: 2rem; }
  .meta { font-size: 0.8rem; color: #86efac; }
  .meta strong { color: #fff; }
  .bracket-btn {
    background: transparent; color: #86efac; border: 1px solid #2a3e2a;
    padding: 7px 16px; border-radius: 20px; font-size: 0.85rem; text-decoration: none;
    transition: all 0.15s;
  }
  .bracket-btn:hover { background: #1a4d2e; color: #fff; border-color: #22c55e; }
  .refresh-btn {
    background: #7cfc00; color: #0a120a; border: none; padding: 8px 18px;
    border-radius: 20px; font-weight: 700; cursor: pointer; font-size: 0.85rem;
    transition: transform 0.1s, background 0.2s;
  }
  .refresh-btn:hover { background: #adff2f; transform: scale(1.04); }

  /* Filter bar */
  .filter-bar {
    background: #0f1a0f; padding: 12px 24px;
    display: flex; gap: 8px; flex-wrap: wrap; align-items: center;
    border-bottom: 1px solid #1a2e1a;
    position: sticky; top: 0; z-index: 10;
  }
  .filter-label { font-size: 0.75rem; color: #4b7a4b; text-transform: uppercase;
                  letter-spacing: 0.05em; margin-right: 4px; }
  .filter-btn {
    background: #162416; color: #86a886; border: 1px solid #2a3e2a;
    padding: 5px 12px; border-radius: 16px; cursor: pointer; font-size: 0.8rem;
    transition: all 0.15s;
  }
  .filter-btn:hover { background: #1e341e; color: #c8e6c8; }
  .filter-btn.active { background: #1a5c1a; color: #fff; border-color: #22c55e; }
  .filter-btn.live-btn.active { background: #dc2626; border-color: #dc2626; color: #fff; }
  .filter-divider { width: 1px; height: 22px; background: #2a3e2a; margin: 0 4px; flex-shrink: 0; }
  .search-wrapper { position: relative; margin-left: auto; }
  .search-input {
    background: #162416; color: #e5e7eb; border: 1px solid #2a3e2a;
    padding: 6px 14px 6px 34px; border-radius: 20px; font-size: 0.85rem;
    outline: none; width: 200px; transition: border-color 0.2s, width 0.2s;
  }
  .search-input:focus { border-color: #22c55e; width: 260px; }
  .search-input::placeholder { color: #4b7a4b; }
  .search-icon { position: absolute; left: 10px; top: 50%; transform: translateY(-50%);
                 color: #4b7a4b; font-size: 0.85rem; pointer-events: none; }

  /* Match cards */
  .content { padding: 0 16px 40px; max-width: 1100px; margin: 0 auto; }
  .day-section { margin-top: 24px; }
  .day-header {
    padding: 10px 16px; background: #0f1a0f;
    border-left: 4px solid #7cfc00; border-radius: 4px;
    font-size: 0.9rem; font-weight: 600; color: #7cfc00;
    margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.04em;
  }
  .match-card {
    background: #0f1a0f; border-radius: 10px; margin-bottom: 6px;
    border: 1px solid #1a2e1a; overflow: hidden; transition: border-color 0.2s;
  }
  .match-card:hover { border-color: #2a3e2a; }
  .match-card.live { border-color: #fbbf24; background: #1a160a; }
  .match-card.finished { opacity: 0.88; }
  .match-inner {
    display: grid;
    grid-template-columns: 90px 1fr auto 1fr 120px;
    align-items: center; padding: 12px 16px; gap: 8px;
  }

  /* Time column */
  .match-time { text-align: center; }
  .match-time .time { font-size: 1.1rem; font-weight: 700; color: #fff; }
  .match-time .trt-label { font-size: 0.65rem; color: #4b7a4b; }
  .match-time .round-tag {
    font-size: 0.68rem; color: #86efac; background: #0d2b0d;
    padding: 2px 6px; border-radius: 10px; margin-top: 3px; display: inline-block;
  }
  .match-time .court-tag {
    font-size: 0.65rem; color: #7cfc00; margin-top: 2px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 86px;
  }

  /* Player columns */
  .player { display: flex; align-items: center; gap: 8px; }
  .player.p1 { justify-content: flex-end; text-align: right; }
  .player.p2 { justify-content: flex-start; text-align: left; }
  .player-name { font-size: 0.95rem; font-weight: 600; color: #e5e7eb; }
  .player-short { font-size: 0.78rem; font-weight: 600; color: #e5e7eb; display: none; }
  .winner .player-name, .winner .player-short { color: #fff; font-weight: 700; }
  .player-flag { width: 24px; height: 16px; object-fit: cover; border-radius: 2px; flex-shrink: 0; }
  .player-flag-ph { width: 24px; height: 16px; background: #2a3e2a; border-radius: 2px; flex-shrink: 0; }

  /* Score box */
  .score-box { text-align: center; flex-shrink: 0; min-width: 90px; }
  .score-display {
    font-size: 1.5rem; font-weight: 800; color: #fff;
    background: #162416; border-radius: 8px;
    padding: 4px 14px; letter-spacing: 2px; display: inline-block;
  }
  .score-display.pending { color: #4b7a4b; font-size: 1rem; letter-spacing: 0; }
  .live .score-display { background: #78350f; color: #fbbf24; }
  .set-detail { font-size: 0.68rem; color: #86a886; margin-top: 3px; letter-spacing: 0.02em; }

  /* Status column */
  .match-status { text-align: center; }
  .status-badge {
    display: inline-block; font-size: 0.7rem; font-weight: 700;
    padding: 3px 10px; border-radius: 12px; text-transform: uppercase; letter-spacing: 0.05em;
  }
  .badge-scheduled { background: #162416; color: #4b7a4b; }
  .badge-live { background: #dc2626; color: #fff; animation: pulse 1.5s infinite; }
  .badge-finished { background: #14532d; color: #86efac; }
  .badge-retired { background: #374151; color: #9ca3af; }
  .badge-suspended { background: #92400e; color: #fcd34d; }
  .live-clock { font-size: 0.8rem; color: #fbbf24; margin-top: 3px; font-weight: 700; }
  .event-label { font-size: 0.68rem; color: #4b7a4b; margin-top: 3px; }
  .venue-text { font-size: 0.65rem; color: #374151; margin-top: 2px; }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.6; } }
  .no-matches { padding: 48px; text-align: center; color: #4b7a4b; font-size: 1rem; }

  /* Responsive */
  @media (max-width: 640px) {
    .match-inner { grid-template-columns: 60px 1fr auto 1fr 80px; padding: 10px; gap: 4px; }
    .player-name { font-size: 0.78rem; display: none; }
    .player-short { display: block; }
    .score-display { font-size: 1.2rem; padding: 3px 10px; }
    .header-title h1 { font-size: 1.1rem; }
    .set-detail { font-size: 0.62rem; }
  }

  /* Theme toggle button */
  .theme-btn {
    background: transparent; color: #86efac; border: 1px solid #2a3e2a;
    padding: 7px 14px; border-radius: 20px; font-size: 0.82rem; cursor: pointer;
    transition: all 0.15s;
  }
  .theme-btn:hover { background: #1a4d2e; color: #fff; }

  /* ===== Navy theme overrides ===== */
  [data-theme="navy"] body { background: #0a0e1a; }
  [data-theme="navy"] .header { background: linear-gradient(135deg,#1a237e 0%,#283593 50%,#1565c0 100%); border-bottom-color: #ffd700; }
  [data-theme="navy"] .header-title .year { color: #ffd700; }
  [data-theme="navy"] .meta { color: #90caf9; }
  [data-theme="navy"] .bracket-btn { color: #90caf9; border-color: #374151; }
  [data-theme="navy"] .bracket-btn:hover { background: #1f2937; border-color: #3b82f6; color: #fff; }
  [data-theme="navy"] .theme-btn { color: #90caf9; border-color: #374151; }
  [data-theme="navy"] .theme-btn:hover { background: #374151; color: #fff; }
  [data-theme="navy"] .refresh-btn { background: #ffd700; color: #000; }
  [data-theme="navy"] .refresh-btn:hover { background: #ffec6e; }
  [data-theme="navy"] .filter-bar { background: #111827; border-bottom-color: #1f2937; }
  [data-theme="navy"] .filter-label { color: #6b7280; }
  [data-theme="navy"] .filter-btn { background: #1f2937; color: #9ca3af; border-color: #374151; }
  [data-theme="navy"] .filter-btn:hover { background: #374151; color: #e5e7eb; }
  [data-theme="navy"] .filter-btn.active { background: #1d4ed8; border-color: #1d4ed8; color: #fff; }
  [data-theme="navy"] .filter-btn.live-btn.active { background: #dc2626; border-color: #dc2626; }
  [data-theme="navy"] .filter-divider { background: #374151; }
  [data-theme="navy"] .search-input { background: #1f2937; border-color: #374151; color: #e5e7eb; }
  [data-theme="navy"] .search-input:focus { border-color: #3b82f6; }
  [data-theme="navy"] .search-input::placeholder { color: #6b7280; }
  [data-theme="navy"] .search-icon { color: #6b7280; }
  [data-theme="navy"] .day-header { background: #111827; border-left-color: #ffd700; color: #ffd700; }
  [data-theme="navy"] .match-card { background: #111827; border-color: #1f2937; }
  [data-theme="navy"] .match-card:hover { border-color: #374151; }
  [data-theme="navy"] .match-card.live { border-color: #fbbf24; background: #131007; }
  [data-theme="navy"] .match-time .trt-label { color: #6b7280; }
  [data-theme="navy"] .round-tag { color: #60a5fa; background: #1e3a5f; }
  [data-theme="navy"] .court-tag { color: #fbd38d; }
  [data-theme="navy"] .player-flag-ph { background: #374151; }
  [data-theme="navy"] .score-display { background: #1f2937; }
  [data-theme="navy"] .score-display.pending { color: #4b5563; }
  [data-theme="navy"] .set-detail { color: #9ca3af; }
  [data-theme="navy"] .match-status .event-label { color: #6b7280; }
  [data-theme="navy"] .badge-scheduled { background: #1f2937; color: #6b7280; }
  [data-theme="navy"] .no-matches { color: #6b7280; }

  /* ===== Slate & Mint theme ===== */
  [data-theme="mint"] body { background: #f1f5f9; color: #0f172a; }
  [data-theme="mint"] .header { background: linear-gradient(135deg,#1e293b 0%,#334155 60%,#1e293b 100%); border-bottom-color: #10b981; }
  [data-theme="mint"] .header-title .year { color: #6ee7b7; }
  [data-theme="mint"] .meta { color: #94a3b8; }
  [data-theme="mint"] .bracket-btn { color: #94a3b8; border-color: #475569; }
  [data-theme="mint"] .bracket-btn:hover { background: #334155; border-color: #10b981; color: #fff; }
  [data-theme="mint"] .theme-btn { color: #94a3b8; border-color: #475569; }
  [data-theme="mint"] .theme-btn:hover { background: #334155; color: #fff; }
  [data-theme="mint"] .refresh-btn { background: #10b981; color: #fff; }
  [data-theme="mint"] .refresh-btn:hover { background: #059669; }
  [data-theme="mint"] .filter-bar { background: #e8edf4; border-bottom-color: #e2e8f0; }
  [data-theme="mint"] .filter-label { color: #64748b; }
  [data-theme="mint"] .filter-btn { background: #f1f5f9; color: #64748b; border-color: #e2e8f0; }
  [data-theme="mint"] .filter-btn:hover { background: #e2e8f0; color: #0f172a; }
  [data-theme="mint"] .filter-btn.active { background: #10b981; border-color: #10b981; color: #fff; }
  [data-theme="mint"] .filter-btn.live-btn.active { background: #dc2626; border-color: #dc2626; }
  [data-theme="mint"] .filter-divider { background: #e2e8f0; }
  [data-theme="mint"] .search-input { background: #fff; border-color: #e2e8f0; color: #0f172a; }
  [data-theme="mint"] .search-input:focus { border-color: #10b981; }
  [data-theme="mint"] .search-input::placeholder { color: #94a3b8; }
  [data-theme="mint"] .search-icon { color: #94a3b8; }
  [data-theme="mint"] .day-header { background: #e8edf4; border-left-color: #10b981; color: #0f766e; }
  [data-theme="mint"] .match-card { background: #fff; border-color: #e2e8f0; }
  [data-theme="mint"] .match-card:hover { border-color: #cbd5e1; }
  [data-theme="mint"] .match-card.live { background: #fffdf5; border-color: #f59e0b; }
  [data-theme="mint"] .match-card.finished { opacity: 0.9; }
  [data-theme="mint"] .match-time .time { color: #0f172a; }
  [data-theme="mint"] .match-time .trt-label { color: #94a3b8; }
  [data-theme="mint"] .round-tag { color: #065f46; background: #d1fae5; }
  [data-theme="mint"] .court-tag { color: #0f766e; }
  [data-theme="mint"] .player-name { color: #0f172a; }
  [data-theme="mint"] .player-short { color: #0f172a; }
  [data-theme="mint"] .winner .player-name, [data-theme="mint"] .winner .player-short { color: #064e3b; }
  [data-theme="mint"] .player-flag-ph { background: #e2e8f0; }
  [data-theme="mint"] .score-display { background: #e8edf4; color: #0f172a; }
  [data-theme="mint"] .live .score-display { background: #fef3c7; color: #92400e; }
  [data-theme="mint"] .score-display.pending { color: #94a3b8; }
  [data-theme="mint"] .set-detail { color: #64748b; }
  [data-theme="mint"] .match-status .event-label { color: #64748b; }
  [data-theme="mint"] .venue-text { color: #94a3b8; }
  [data-theme="mint"] .badge-scheduled { background: #e8edf4; color: #64748b; }
  [data-theme="mint"] .badge-finished { background: #d1fae5; color: #065f46; }
  [data-theme="mint"] .no-matches { color: #64748b; }
</style>
</head>
<body>

<div class="header">
  <div class="header-title">
    <span class="trophy">🎾</span>
    <div>
      <h1>Wimbledon <span class="year">2026</span></h1>
      <div class="meta">Son güncelleme: <strong>__UPDATED__</strong></div>
    </div>
  </div>
  <a href="/bracket?event=mens" class="bracket-btn">&#9776; Tur Tablosu</a>
  <button class="theme-btn" id="themeBtn" onclick="cycleTheme()">&#9681; Tema</button>
  <button class="refresh-btn" onclick="location.reload()">&#8635; Yenile</button>
</div>

<div class="filter-bar">
  <span class="filter-label">Filtre:</span>
  <button class="filter-btn active" onclick="filterMatches('all', this)">Tümü</button>
  <button class="filter-btn live-btn" onclick="filterMatches('live', this)">🔴 Canlı</button>
  <button class="filter-btn" onclick="filterMatches('today', this)">Bugün</button>
  <div class="filter-divider"></div>
  <button class="filter-btn" onclick="filterMatches('erkekler-tekler', this)">Erkekler</button>
  <button class="filter-btn" onclick="filterMatches('kadinlar-tekler', this)">Kadınlar</button>
  <div class="filter-divider"></div>
  <button class="filter-btn" onclick="filterMatches('1. Tur', this)">1T</button>
  <button class="filter-btn" onclick="filterMatches('2. Tur', this)">2T</button>
  <button class="filter-btn" onclick="filterMatches('3. Tur', this)">3T</button>
  <button class="filter-btn" onclick="filterMatches('4. Tur', this)">4T</button>
  <button class="filter-btn" onclick="filterMatches('Çeyrek Final', this)">ÇF</button>
  <button class="filter-btn" onclick="filterMatches('Yarı Final', this)">YF</button>
  <button class="filter-btn" onclick="filterMatches('Final', this)">Final</button>
  <div class="search-wrapper">
    <span class="search-icon">&#128269;</span>
    <input class="search-input" id="searchInput" type="text" placeholder="Oyuncu ara..."
           oninput="searchPlayer(this.value)">
  </div>
</div>

<div class="content" id="matchView">
  __MATCH_CONTENT__
</div>

<script>
  const TODAY = "__TODAY__";
  const ROUND_ORDER = ['1. Tur','2. Tur','3. Tur','4. Tur','Çeyrek Final','Yarı Final','Final'];

  // --- Match filtering ---

  function applyVisibility(cards, daySections) {
    daySections.forEach(function(section) {
      const visible = Array.from(section.querySelectorAll('.match-card')).some(c => c.style.display !== 'none');
      section.style.display = visible ? '' : 'none';
    });
  }

  function filterMatches(filter, btn) {
    document.getElementById('searchInput').value = '';
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    const cards = document.querySelectorAll('.match-card');
    const daySections = document.querySelectorAll('.day-section');
    const roundIdx = ROUND_ORDER.indexOf(filter);
    cards.forEach(function(card) {
      let show = false;
      if (filter === 'all') show = true;
      else if (filter === 'live') show = card.dataset.state === 'in';
      else if (filter === 'today') show = card.dataset.date === TODAY;
      else if (filter === 'erkekler-tekler' || filter === 'kadinlar-tekler')
        show = card.dataset.event === filter;
      else if (roundIdx >= 0)
        show = ROUND_ORDER.indexOf(card.dataset.round) >= roundIdx;
      card.style.display = show ? '' : 'none';
    });
    applyVisibility(cards, daySections);
  }

  function searchPlayer(query) {
    const q = query.trim().toLowerCase();
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
    if (!q) document.querySelector('.filter-btn').classList.add('active');
    const cards = document.querySelectorAll('.match-card');
    const daySections = document.querySelectorAll('.day-section');
    cards.forEach(function(card) {
      card.style.display = (!q || card.dataset.p1.includes(q) || card.dataset.p2.includes(q)) ? '' : 'none';
    });
    applyVisibility(cards, daySections);
  }

  // --- Scroll to today on load ---
  document.addEventListener('DOMContentLoaded', function() {
    const todayCard = document.querySelector('.match-card[data-date="' + TODAY + '"]');
    if (todayCard) {
      const section = todayCard.closest('.day-section');
      if (section) {
        const y = section.getBoundingClientRect().top + window.scrollY - 60;
        window.scrollTo({ top: y, behavior: 'smooth' });
      }
    }
  });

  // --- Theme ---
  var _themes = ['navy', 'mint', 'green'];
  var _theme = localStorage.getItem('wim-theme') || 'navy';
  function _applyTheme(t) {
    _theme = t;
    document.documentElement.setAttribute('data-theme', t === 'green' ? '' : t);
    localStorage.setItem('wim-theme', t);
  }
  function cycleTheme() { _applyTheme(_themes[(_themes.indexOf(_theme) + 1) % _themes.length]); }
  _applyTheme(_theme);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# HTML builder
# ---------------------------------------------------------------------------

def build_html(matches: list[dict]) -> str:
    now_trt = datetime.now(TRT)
    today_str = now_trt.strftime("%Y-%m-%d")
    updated_str = now_trt.strftime("%H:%M TRT")

    by_date: dict[str, list] = {}
    for m in matches:
        by_date.setdefault(m["date_trt"], []).append(m)

    DAY_TR = {
        "Monday": "Pazartesi", "Tuesday": "Salı", "Wednesday": "Çarşamba",
        "Thursday": "Perşembe", "Friday": "Cuma", "Saturday": "Cumartesi", "Sunday": "Pazar",
    }
    MONTH_TR = {
        "Jan": "Oca", "Feb": "Şub", "Mar": "Mar", "Apr": "Nis",
        "May": "May", "Jun": "Haz", "Jul": "Tem", "Aug": "Ağu",
    }

    sections_html = []
    for date_key in sorted(by_date.keys()):
        day_matches = by_date[date_key]
        dt = datetime.strptime(date_key, "%Y-%m-%d")
        day_en = dt.strftime("%A")
        date_parts = dt.strftime("%d %b").split(" ")
        day_tr = DAY_TR.get(day_en, day_en)
        month_tr = MONTH_TR.get(date_parts[1], date_parts[1])
        today_marker = " — Bugün" if date_key == today_str else ""
        header = f"{day_tr}, {date_parts[0]} {month_tr}{today_marker}"

        cards_html = []
        for m in day_matches:
            state = m["state"]
            card_class = "match-card" + (" live" if state == "in" else " finished" if state == "post" else "")
            court_html = f'<div class="court-tag">{m["court"]}</div>' if m["court"] else ""
            card = (
                f'<div class="{card_class}"'
                f' data-date="{m["date_trt"]}"'
                f' data-state="{state}"'
                f' data-event="{m["event_key"]}"'
                f' data-round="{m["round_label"]}"'
                f' data-p1="{m["p1_name"].lower()}"'
                f' data-p2="{m["p2_name"].lower()}">'
                f'<div class="match-inner">'
                f'<div class="match-time">'
                f'<div class="time">{m["time_trt"]}</div>'
                f'<div class="trt-label">TRT</div>'
                f'<div class="round-tag">{m["round_label"]}</div>'
                f'{court_html}'
                f'</div>'
                f'{_player_html(m, "p1")}'
                f'{_score_html(m)}'
                f'{_player_html(m, "p2")}'
                f'<div class="match-status">'
                f'{_badge(state, m["status"], m["live_clock"])}'
                f'<div class="event-label">{m["event_label"]}</div>'
                f'<div class="venue-text">{m["venue"]}</div>'
                f'</div>'
                f'</div></div>'
            )
            cards_html.append(card)

        sections_html.append(
            f'<div class="day-section"><div class="day-header">{header}</div>'
            + "\n".join(cards_html) + "</div>"
        )

    match_content = "\n".join(sections_html) if sections_html else '<div class="no-matches">Maç bulunamadı.</div>'

    html = HTML_TEMPLATE
    html = html.replace("__UPDATED__", updated_str)
    html = html.replace("__TODAY__", today_str)
    html = html.replace("__MATCH_CONTENT__", match_content)
    return html


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return redirect("/bracket?event=mens")


@app.route("/matches")
def matches_page():
    matches = get_matches()
    return Response(build_html(matches), mimetype="text/html; charset=utf-8")


@app.route("/api/scores")
def api_scores():
    return jsonify(get_matches())


# ---------------------------------------------------------------------------
# Bracket view
# ---------------------------------------------------------------------------

BRACKET_ROUNDS = [
    ("Round 1",      "1. Tur"),
    ("Round 2",      "2. Tur"),
    ("Round 3",      "3. Tur"),
    ("Round 4",      "4. Tur"),
    ("Quarterfinal", "Çeyrek Final"),
    ("Semifinal",    "Yarı Final"),
    ("Final",        "Final"),
]
ROUND_SIZES = [64, 32, 16, 8, 4, 2, 1]
BRACKET_H = 5600     # px: total column height
MATCH_H = 76         # px: height of one match card (2 × 28px rows + 20px detail)
COL_W = 188          # px: each round column width
COL_GAP = 32         # px: gap between round columns (room for connector lines)


def _build_bracket_cols(matches: list[dict], event_key: str) -> list[list[dict]]:
    """Return 7 lists (one per round) in correct bracket slot order.

    ESPN assigns match IDs by court schedule, not bracket position, and its API
    does not expose draw positions. When we have the official draw for an event
    (see draw_data.py), we order each round by the lowest draw position present
    in the match — this reproduces the real bracket exactly. Otherwise we fall
    back to seed + player-ID reconstruction.
    """
    evt = [m for m in matches if m["event_key"] == event_key]

    raw_cols = []
    for rnd_raw, _ in BRACKET_ROUNDS:
        rnd_m = [m for m in evt if m["round_raw"] == rnd_raw]
        raw_cols.append(rnd_m)

    # Preferred: order by official draw position.
    draw_idx = DRAW_INDEX.get(event_key)
    if draw_idx and draw_idx[0]:
        by_sur, by_tok = draw_idx

        def _draw_anchor(m: dict) -> int:
            positions = []
            for side in ("p1", "p2"):
                pos = _draw_position(m.get(f"{side}_short", ""),
                                     m.get(f"{side}_name", ""), by_sur, by_tok)
                if pos:
                    positions.append(pos)
            return min(positions) if positions else 9999

        return [sorted(col, key=lambda m: (_draw_anchor(m), int(m["match_id"])))
                for col in raw_cols]

    return _build_bracket_cols_fallback(raw_cols)


def _build_bracket_cols_fallback(raw_cols: list[list[dict]]) -> list[list[dict]]:
    """Seed + player-ID bracket reconstruction (used when no draw data exists)."""

    def _seed_key(m: dict) -> tuple:
        ranks = [r for r in (m.get("p1_rank"), m.get("p2_rank")) if r]
        return (min(ranks) if ranks else 999, int(m["match_id"]))

    def _pid_map(rnd: list[dict]) -> dict:
        pm = {}
        for m in rnd:
            for side in ("p1", "p2"):
                pid = m.get(f"{side}_id")
                # Exclude TBD placeholder IDs (ESPN uses negative IDs like "-4", "-3")
                if pid and pid.lstrip("-").isdigit() and not pid.startswith("-"):
                    pm[pid] = m
        return pm

    # Sort each round by seed first (best available ordering for TBD matches)
    seed_sorted = [sorted(col, key=_seed_key) for col in raw_cols]

    # Now refine using player-ID linkage: work backwards from Final to R1.
    # For each match in round N, find its two feeder matches in round N-1.
    ordered = [None] * 7
    ordered[6] = seed_sorted[6]  # Final: 1 match, seed-sorted is fine

    for ri in range(5, -1, -1):
        parent_col = ordered[ri + 1]
        prev_raw = raw_cols[ri]

        if not prev_raw:
            ordered[ri] = []
            continue

        if not parent_col:
            ordered[ri] = seed_sorted[ri]
            continue

        pid_map = _pid_map(prev_raw)
        result = []
        used = set()

        for parent in parent_col:
            feeders = []
            for side in ("p1", "p2"):
                pid = parent.get(f"{side}_id")
                if pid and pid in pid_map:
                    feeder = pid_map[pid]
                    if feeder["match_id"] not in used:
                        feeders.append(feeder)
                        used.add(feeder["match_id"])
            feeders.sort(key=_seed_key)
            result.extend(feeders)

        # Append unlinked matches (TBD — can't determine slot yet) sorted by seed
        unlinked = [m for m in prev_raw if m["match_id"] not in used]
        unlinked.sort(key=_seed_key)
        result.extend(unlinked)

        ordered[ri] = result

    return ordered


def _bracket_card(m: dict) -> str:
    state = m["state"]
    is_live = state == "in"
    is_post = state == "post"

    def _row(side: str) -> str:
        name = m[f"{side}_short"] or m[f"{side}_name"] or "TBD"
        seed = m.get(f"{side}_seed")
        flag = m[f"{side}_flag"]
        flag_alt = m[f"{side}_flag_alt"]
        sets = m[f"{side}_sets"]
        winner = m[f"{side}_winner"] and is_post

        seed_html = f'<span class="bseed">{seed}</span>' if seed else ""
        flag_html = f'<img class="bflag" src="{flag}" alt="{flag_alt}" loading="lazy">' if flag else '<div class="bflag-ph"></div>'
        sets_html = ""
        if sets is not None:
            cls = "bsets-live" if is_live else ("bsets-win" if winner else "bsets")
            sets_html = f'<span class="{cls}">{sets}</span>'
        row_cls = "brow winner" if winner else "brow"
        name_cls = "bname"
        return (
            f'<div class="{row_cls}">'
            f'{flag_html}{seed_html}'
            f'<span class="{name_cls}">{name}</span>'
            f'{sets_html}'
            f'</div>'
        )

    card_cls = "bm live" if is_live else ("bm done" if is_post else "bm")
    live_dot = '<span class="live-dot"></span>' if is_live else ""
    detail = m.get("set_detail", "")
    detail_html = f'<div class="bdetail">{detail}</div>' if detail else '<div class="bdetail"></div>'
    return (
        f'<div class="{card_cls}">'
        f'{live_dot}'
        f'{_row("p1")}'
        f'{_row("p2")}'
        f'{detail_html}'
        f'</div>'
    )


def _bracket_tbd() -> str:
    return (
        '<div class="bm tbd">'
        '<div class="brow"><span class="bname tbd-name">TBD</span></div>'
        '<div class="brow"><span class="bname tbd-name">TBD</span></div>'
        '</div>'
    )


def _build_bracket_col_html(idx: int, matches: list[dict], expected: int) -> str:
    cards = []
    for m in matches:
        cards.append(_bracket_card(m))
    # Pad with TBD cards if fewer matches than expected (shouldn't happen in a full draw)
    while len(cards) < expected:
        cards.append(_bracket_tbd())
    return "\n".join(cards)


BRACKET_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Wimbledon 2026 — Tur Tablosu</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background: #0a120a; color: #e8eaf6; min-height: 100vh; }

  .header {
    background: linear-gradient(135deg, #0d2b0d 0%, #1a4d2e 50%, #2d1a42 100%);
    padding: 16px 24px; display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
    border-bottom: 3px solid #7cfc00; position: sticky; top: 0; z-index: 20;
  }
  .back-btn {
    background: #162416; color: #86efac; border: 1px solid #2a3e2a;
    padding: 6px 14px; border-radius: 16px; text-decoration: none; font-size: 0.85rem;
    transition: background 0.2s; flex-shrink: 0;
  }
  .back-btn:hover { background: #1e341e; }
  .header-title { font-size: 1.1rem; font-weight: 700; color: #fff; flex: 1; }
  .header-title span { color: #7cfc00; }
  .event-tabs { display: flex; gap: 8px; margin-left: auto; }
  .etab {
    background: #162416; color: #86a886; border: 1px solid #2a3e2a;
    padding: 6px 16px; border-radius: 16px; text-decoration: none; font-size: 0.82rem;
    transition: all 0.15s;
  }
  .etab:hover { background: #1e341e; color: #c8e6c8; }
  .etab.active { background: #1a5c1a; color: #fff; border-color: #22c55e; }
  .meta { font-size: 0.75rem; color: #86efac; }

  /* Bracket layout */
  .bracket-outer { overflow: auto; padding: 0 16px 40px; }
  .rh-wrapper {
    position: sticky; top: 99px; z-index: 10;
    background: #0a120a; overflow: hidden; padding: 0 16px;
    border-bottom: 1px solid #1a2e1a;
  }
  .rh-row { display: flex; gap: __COL_GAP__px; padding: 10px 0 8px; width: max-content; }
  .rh { width: __COL_W__px; text-align: center; font-size: 0.72rem; font-weight: 700;
        color: #7cfc00; text-transform: uppercase; letter-spacing: 0.06em; }
  .bracket-body { display: flex; gap: __COL_GAP__px; position: relative;
                  width: max-content; min-height: __BRACKET_H__px; }
  .round-col { width: __COL_W__px; min-height: __BRACKET_H__px;
               display: flex; flex-direction: column; justify-content: space-around; }

  /* Match cards */
  .bm {
    width: __COL_W__px; height: __MATCH_H__px; flex: none;
    background: #0f1a0f; border: 1px solid #1a2e1a; border-radius: 6px;
    overflow: hidden; position: relative; transition: border-color 0.2s;
  }
  .bm:hover { border-color: #2a3e2a; }
  .bm.live { border-color: #fbbf24; background: #1a160a; }
  .bm.done { }
  .bm.tbd { opacity: 0.4; }
  .live-dot {
    position: absolute; right: 5px; top: 28px; transform: translateY(-50%);
    width: 6px; height: 6px; border-radius: 50%; background: #dc2626;
    animation: pulse 1.2s infinite;
  }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }

  .brow {
    height: 28px; display: flex; align-items: center; gap: 5px;
    padding: 0 8px; border-bottom: 1px solid #0a120a; overflow: hidden;
  }
  .brow:last-child { border-bottom: none; }
  .brow.winner .bname { color: #fff; font-weight: 700; }
  .brow.winner .bsets, .brow.winner .bsets-win { font-weight: 800; }

  .bflag { width: 18px; height: 12px; object-fit: cover; border-radius: 2px; flex-shrink: 0; }
  .bflag-ph { width: 18px; height: 12px; flex-shrink: 0; }
  .bseed {
    font-size: 0.62rem; color: #86efac; background: #0d2b0d;
    border-radius: 8px; padding: 0px 4px; flex-shrink: 0; line-height: 1.5;
  }
  .bname {
    font-size: 0.76rem; color: #9ca3af; flex: 1;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .tbd-name { color: #374151; font-style: italic; }
  .bsets { font-size: 0.78rem; font-weight: 600; color: #6b7280; flex-shrink: 0; }
  .bsets-win { font-size: 0.78rem; font-weight: 700; color: #86efac; flex-shrink: 0; }
  .bsets-live { font-size: 0.78rem; font-weight: 700; color: #fbbf24; flex-shrink: 0; }
  .bdetail {
    height: 20px; display: flex; align-items: center; justify-content: center;
    font-size: 0.72rem; font-weight: 600; color: #86a886;
    border-top: 1px solid #111a11; overflow: hidden; white-space: nowrap;
    letter-spacing: 0.03em;
  }
  .bm.live .bdetail { color: #fbbf24; background: rgba(251,191,36,0.08); }
  .bm.done .bdetail { color: #a3c4a3; }

  /* Round filter bar */
  .bracket-filter {
    background: #0f1a0f; padding: 8px 24px;
    display: flex; gap: 6px; align-items: center;
    border-bottom: 1px solid #1a2e1a;
    position: sticky; top: 61px; z-index: 15;
  }
  .bfilt-label { font-size: 0.75rem; color: #4b7a4b; text-transform: uppercase;
                 letter-spacing: 0.05em; margin-right: 4px; }
  .bfilt-btn {
    background: #162416; color: #86a886; border: 1px solid #2a3e2a;
    padding: 4px 11px; border-radius: 14px; cursor: pointer; font-size: 0.78rem;
    transition: all 0.15s;
  }
  .bfilt-btn:hover { background: #1e341e; color: #c8e6c8; }
  .bfilt-btn.active { background: #1a5c1a; color: #fff; border-color: #22c55e; }

  /* SVG connector lines */
  #blines { position: absolute; top: 0; left: 0; overflow: visible; pointer-events: none; }

  /* Theme toggle button */
  .theme-btn {
    background: #162416; color: #86a886; border: 1px solid #2a3e2a;
    padding: 6px 14px; border-radius: 16px; font-size: 0.82rem; cursor: pointer;
    transition: all 0.15s;
  }
  .theme-btn:hover { background: #1e341e; color: #c8e6c8; }

  /* ===== Navy theme overrides ===== */
  [data-theme="navy"] body { background: #0a0e1a; }
  [data-theme="navy"] .header { background: linear-gradient(135deg,#1a237e 0%,#283593 50%,#1565c0 100%); border-bottom-color: #ffd700; }
  [data-theme="navy"] .header-title { color: #fff; }
  [data-theme="navy"] .header-title span { color: #ffd700; }
  [data-theme="navy"] .meta { color: #90caf9; }
  [data-theme="navy"] .back-btn { background: #1f2937; color: #90caf9; border-color: #374151; }
  [data-theme="navy"] .back-btn:hover { background: #374151; }
  [data-theme="navy"] .etab { background: #1f2937; color: #9ca3af; border-color: #374151; }
  [data-theme="navy"] .etab:hover { background: #374151; color: #e5e7eb; }
  [data-theme="navy"] .etab.active { background: #1d4ed8; border-color: #1d4ed8; color: #fff; }
  [data-theme="navy"] .theme-btn { background: #1f2937; color: #90caf9; border-color: #374151; }
  [data-theme="navy"] .theme-btn:hover { background: #374151; color: #fff; }
  [data-theme="navy"] .bracket-filter { background: #111827; border-bottom-color: #1f2937; }
  [data-theme="navy"] .bfilt-label { color: #6b7280; }
  [data-theme="navy"] .bfilt-btn { background: #1f2937; color: #9ca3af; border-color: #374151; }
  [data-theme="navy"] .bfilt-btn:hover { background: #374151; color: #e5e7eb; }
  [data-theme="navy"] .bfilt-btn.active { background: #1d4ed8; border-color: #1d4ed8; color: #fff; }
  [data-theme="navy"] .rh-wrapper { background: #0a0e1a; border-bottom-color: #1f2937; }
  [data-theme="navy"] .rh { color: #60a5fa; }
  [data-theme="navy"] .bm { background: #111827; border-color: #1f2937; }
  [data-theme="navy"] .bm:hover { border-color: #374151; }
  [data-theme="navy"] .bm.live { border-color: #fbbf24; background: #131007; }
  [data-theme="navy"] .bm.tbd { opacity: 0.35; }
  [data-theme="navy"] .brow { border-bottom-color: #0a0e1a; }
  [data-theme="navy"] .bseed { color: #60a5fa; background: #1e3a5f; }
  [data-theme="navy"] .bdetail { border-top-color: #111827; }
  [data-theme="navy"] .bm.done .bdetail { color: #9ca3af; }

  /* ===== Slate & Mint theme (bracket) ===== */
  [data-theme="mint"] body { background: #f1f5f9; }
  [data-theme="mint"] .header { background: linear-gradient(135deg,#1e293b 0%,#334155 60%,#1e293b 100%); border-bottom-color: #10b981; }
  [data-theme="mint"] .header-title { color: #fff; }
  [data-theme="mint"] .header-title span { color: #6ee7b7; }
  [data-theme="mint"] .meta { color: #94a3b8; }
  [data-theme="mint"] .back-btn { background: #e8edf4; color: #475569; border-color: #e2e8f0; }
  [data-theme="mint"] .back-btn:hover { background: #e2e8f0; }
  [data-theme="mint"] .etab { background: #e8edf4; color: #64748b; border-color: #e2e8f0; }
  [data-theme="mint"] .etab:hover { background: #e2e8f0; color: #0f172a; }
  [data-theme="mint"] .etab.active { background: #10b981; border-color: #10b981; color: #fff; }
  [data-theme="mint"] .theme-btn { background: #e8edf4; color: #475569; border-color: #e2e8f0; }
  [data-theme="mint"] .theme-btn:hover { background: #e2e8f0; color: #0f172a; }
  [data-theme="mint"] .bracket-filter { background: #e8edf4; border-bottom-color: #e2e8f0; }
  [data-theme="mint"] .bfilt-label { color: #64748b; }
  [data-theme="mint"] .bfilt-btn { background: #f1f5f9; color: #64748b; border-color: #e2e8f0; }
  [data-theme="mint"] .bfilt-btn:hover { background: #e2e8f0; color: #0f172a; }
  [data-theme="mint"] .bfilt-btn.active { background: #10b981; border-color: #10b981; color: #fff; }
  [data-theme="mint"] .rh-wrapper { background: #f1f5f9; border-bottom-color: #e2e8f0; }
  [data-theme="mint"] .rh { color: #0f766e; }
  [data-theme="mint"] .bm { background: #fff; border-color: #e2e8f0; }
  [data-theme="mint"] .bm:hover { border-color: #cbd5e1; }
  [data-theme="mint"] .bm.live { background: #fffdf5; border-color: #f59e0b; }
  [data-theme="mint"] .bm.tbd { opacity: 0.4; }
  [data-theme="mint"] .brow { border-bottom-color: #f1f5f9; }
  [data-theme="mint"] .brow.winner .bname { color: #064e3b; }
  [data-theme="mint"] .bname { color: #475569; }
  [data-theme="mint"] .bseed { color: #065f46; background: #d1fae5; }
  [data-theme="mint"] .bsets { color: #94a3b8; }
  [data-theme="mint"] .bsets-win { color: #065f46; }
  [data-theme="mint"] .bsets-live { color: #b45309; }
  [data-theme="mint"] .bdetail { border-top-color: #f1f5f9; }
  [data-theme="mint"] .bm.done .bdetail { color: #64748b; }
  [data-theme="mint"] .bm.live .bdetail { color: #b45309; background: rgba(180,83,9,0.06); }
</style>
</head>
<body>

<div class="header">
  <a href="/matches" class="back-btn">&#8592; Maçlar</a>
  <div class="header-title">&#127938; Wimbledon <span>2026</span> — Tur Tablosu</div>
  <div class="meta">__UPDATED__</div>
  <div class="event-tabs">
    <button class="theme-btn" id="themeBtn" onclick="cycleTheme()">&#9681; Tema</button>
    <a href="/bracket?event=mens" class="etab __MENS_ACTIVE__">Erkekler</a>
    <a href="/bracket?event=womens" class="etab __WOMENS_ACTIVE__">Kadınlar</a>
  </div>
</div>

<div class="bracket-filter">
  <span class="bfilt-label">Tur:</span>
  <button class="bfilt-btn active" onclick="filterBracketRound(0, this)">1T</button>
  <button class="bfilt-btn" onclick="filterBracketRound(1, this)">2T</button>
  <button class="bfilt-btn" onclick="filterBracketRound(2, this)">3T</button>
  <button class="bfilt-btn" onclick="filterBracketRound(3, this)">4T</button>
  <button class="bfilt-btn" onclick="filterBracketRound(4, this)">ÇF</button>
  <button class="bfilt-btn" onclick="filterBracketRound(5, this)">YF</button>
  <button class="bfilt-btn" onclick="filterBracketRound(6, this)">Final</button>
</div>

<div class="rh-wrapper">
  <div class="rh-row" id="rhRow">
    __ROUND_HEADERS__
  </div>
</div>

<div class="bracket-outer" id="bracketOuter">
  <div class="bracket-body" id="bracket">
    __BRACKET_COLS__
    <svg id="blines"></svg>
  </div>
</div>

<script>
function drawLines() {
  var bracket = document.getElementById('bracket');
  var svg = document.getElementById('blines');
  svg.innerHTML = '';
  var br = bracket.getBoundingClientRect();
  svg.style.width = br.width + 'px';
  svg.style.height = br.height + 'px';

  var rounds = Array.from(bracket.querySelectorAll('.round-col')).filter(function(r) {
    return r.style.display !== 'none';
  });
  var _th = document.documentElement.getAttribute('data-theme') || 'green';
  var color = _th === 'mint' ? '#cbd5e1' : (_th === 'navy' ? '#1e3a5f' : '#1e3a1e');

  function makeLine(x1, y1, x2, y2) {
    var l = document.createElementNS('http://www.w3.org/2000/svg','line');
    l.setAttribute('x1', x1.toFixed(1)); l.setAttribute('y1', y1.toFixed(1));
    l.setAttribute('x2', x2.toFixed(1)); l.setAttribute('y2', y2.toFixed(1));
    l.setAttribute('stroke', color); l.setAttribute('stroke-width', '1.5');
    svg.appendChild(l);
  }

  for (var r = 0; r < rounds.length - 1; r++) {
    var fromCards = Array.from(rounds[r].querySelectorAll('.bm'));
    var toCards = Array.from(rounds[r+1].querySelectorAll('.bm'));
    for (var i = 0; i < toCards.length; i++) {
      var m1 = fromCards[i*2];
      var m2 = fromCards[i*2+1];
      var mTo = toCards[i];
      if (!m1 || !m2 || !mTo) continue;
      var r1 = m1.getBoundingClientRect();
      var r2 = m2.getBoundingClientRect();
      var rTo = mTo.getBoundingClientRect();
      var y1 = r1.top + r1.height/2 - br.top;
      var y2 = r2.top + r2.height/2 - br.top;
      var yTo = rTo.top + rTo.height/2 - br.top;
      var xFrom = r1.right - br.left;
      var xTo = rTo.left - br.left;
      var xMid = xFrom + (xTo - xFrom) / 2;
      makeLine(xFrom, y1, xMid, y1);
      makeLine(xFrom, y2, xMid, y2);
      makeLine(xMid, y1, xMid, y2);
      makeLine(xMid, yTo, xTo, yTo);
    }
  }
}

function filterBracketRound(fromIdx, btn) {
  document.querySelectorAll('.bfilt-btn').forEach(function(b) { b.classList.remove('active'); });
  btn.classList.add('active');
  // Card slot = MATCH_H(76) + desired gap(10) = 86px
  // With space-around and half the items in same height, each round stays
  // automatically centered between its predecessor pairs.
  var SIZES = [64, 32, 16, 8, 4, 2, 1];
  var newH = fromIdx > 0 ? (SIZES[fromIdx] * 86) + 'px' : '';
  document.querySelectorAll('.round-col').forEach(function(col, i) {
    col.style.display = i >= fromIdx ? '' : 'none';
    col.style.minHeight = newH;
  });
  document.querySelectorAll('.rh').forEach(function(rh, i) {
    rh.style.display = i >= fromIdx ? '' : 'none';
  });
  document.getElementById('bracket').style.minHeight = newH;
  drawLines();
}

window.addEventListener('load', drawLines);

// Sync round header row with horizontal scroll of bracket
document.getElementById('bracketOuter').addEventListener('scroll', function() {
  document.getElementById('rhRow').style.transform = 'translateX(-' + this.scrollLeft + 'px)';
});

// --- Theme ---
var _themes = ['navy', 'mint', 'green'];
var _theme = localStorage.getItem('wim-theme') || 'navy';
function _applyTheme(t) {
  _theme = t;
  document.documentElement.setAttribute('data-theme', t === 'green' ? '' : t);
  localStorage.setItem('wim-theme', t);
}
function cycleTheme() { _applyTheme(_themes[(_themes.indexOf(_theme) + 1) % _themes.length]); }
_applyTheme(_theme);
</script>
</body>
</html>
"""


def build_bracket_page(matches: list[dict], event_key: str) -> str:
    cols = _build_bracket_cols(matches, event_key)

    round_hdrs = "".join(
        f'<div class="rh">{lbl}</div>' for _, lbl in BRACKET_ROUNDS
    )

    col_parts = []
    for idx, (rnd, _) in enumerate(BRACKET_ROUNDS):
        expected = ROUND_SIZES[idx]
        col_html = _build_bracket_col_html(idx, cols[idx], expected)
        col_parts.append(f'<div class="round-col" data-round="{rnd}">\n{col_html}\n</div>')

    now_trt = datetime.now(TRT)
    updated = now_trt.strftime("%H:%M TRT")

    html = BRACKET_HTML_TEMPLATE
    html = html.replace("__COL_W__", str(COL_W))
    html = html.replace("__COL_GAP__", str(COL_GAP))
    html = html.replace("__BRACKET_H__", str(BRACKET_H))
    html = html.replace("__MATCH_H__", str(MATCH_H))
    html = html.replace("__ROUND_HEADERS__", round_hdrs)
    html = html.replace("__BRACKET_COLS__", "\n".join(col_parts))
    html = html.replace("__UPDATED__", updated)
    html = html.replace("__MENS_ACTIVE__", "active" if event_key == "mens" else "")
    html = html.replace("__WOMENS_ACTIVE__", "active" if event_key == "womens" else "")
    return html


@app.route("/bracket")
def bracket():
    event = request.args.get("event", "mens")
    event_key = "erkekler-tekler" if event == "mens" else "kadinlar-tekler"
    matches = get_matches()
    return Response(build_bracket_page(matches, event_key), mimetype="text/html; charset=utf-8")


if __name__ == "__main__":
    print("=" * 50)
    print("  Wimbledon 2026 Tenis Takip Sistemi")
    print("  http://localhost:5000 adresini açın")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5000, debug=False)
