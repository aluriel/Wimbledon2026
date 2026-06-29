#!/usr/bin/env python3
"""
ESPN public API'den Wimbledon 2026 maç verilerini çeker.
Erkekler ve Kadınlar Tekler ana tur maçlarını içerir.
Maç saatlerini TRT'ye (UTC+3) çevirir.
Yapılandırılmış JSON'ı .tmp/wimbledon_scores.json dosyasına kaydeder.

Kullanım:
    python tools/fetch_scores.py
"""

import json
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

TRT = timezone(timedelta(hours=3), name="TRT")

ESPN_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/tennis/atp/scoreboard?limit=300"
)

OUTPUT_PATH = Path(__file__).parent.parent / ".tmp" / "wimbledon_scores.json"

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


def _build_set_detail(p1_ls: list, p2_ls: list) -> str:
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

    raw = sorted(comp.get("competitors", []), key=lambda c: c.get("order", 99))
    p1_raw = raw[0] if raw else {}
    p2_raw = raw[1] if len(raw) > 1 else {}

    def _player(c: dict) -> dict:
        athlete = c.get("athlete", {})
        flag = athlete.get("flag", {})
        return {
            "id":       str(c.get("id", "")),
            "name":     athlete.get("displayName", ""),
            "short":    athlete.get("shortName", athlete.get("displayName", "")),
            "flag":     flag.get("href", ""),
            "flag_alt": flag.get("alt", ""),
            "winner":   c.get("winner", False),
        }

    p1 = _player(p1_raw)
    p2 = _player(p2_raw)

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
        "p1_sets":     p1_sets,
        "p1_winner":   p1["winner"],
        "p2_id":       p2["id"],
        "p2_name":     p2["name"],
        "p2_short":    p2["short"],
        "p2_flag":     p2["flag"],
        "p2_flag_alt": p2["flag_alt"],
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


def fetch_all() -> list[dict]:
    try:
        resp = requests.get(ESPN_URL, timeout=20, headers={"User-Agent": "WimbledonTracker/1.0"})
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as e:
        print(f"[HATA] ESPN API: {e}")
        return []

    wimbledon = next(
        (e for e in data.get("events", []) if "wimbledon" in e.get("name", "").lower()),
        None,
    )
    if not wimbledon:
        print("[UYARI] ESPN yanıtında Wimbledon bulunamadı")
        return []

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
    return matches


def main() -> None:
    print("Wimbledon 2026 maçları yükleniyor...")

    matches = fetch_all()
    if not matches:
        print("Maç bulunamadı.")
        return

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(matches, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    total = len(matches)
    finished = sum(1 for m in matches if m["state"] == "post")
    live = sum(1 for m in matches if m["state"] == "in")
    scheduled = sum(1 for m in matches if m["state"] == "pre")
    mens = sum(1 for m in matches if m["event_key"] == "erkekler-tekler")
    womens = sum(1 for m in matches if m["event_key"] == "kadinlar-tekler")

    print(f"  {total} maç: {finished} bitti, {live} canlı, {scheduled} planlandı")
    print(f"  Erkekler: {mens} maç  |  Kadınlar: {womens} maç")
    print(f"  Kaydedildi: {OUTPUT_PATH}")

    today_trt = datetime.now(TRT).strftime("%Y-%m-%d")
    today_matches = [m for m in matches if m["date_trt"] == today_trt]
    if today_matches:
        print(f"\n  Bugünkü maçlar (TRT):")
        for m in today_matches:
            sets_str = f"{m['p1_sets']}-{m['p2_sets']}" if m["p1_sets"] is not None else "vs"
            detail = f"  [{m['set_detail']}]" if m["set_detail"] else ""
            state_icon = "[CANLI]" if m["state"] == "in" else ("[BITTI]" if m["state"] == "post" else "[  ]")
            print(f"    {state_icon} [{m['time_trt']} TRT] {m['p1_name']} {sets_str} {m['p2_name']}{detail}")
            print(f"              {m['event_label']} — {m['round_label']} — {m['court'] or 'Kort belirtilmedi'}")
    else:
        print("\n  Bugün ana tur maçı yok.")


if __name__ == "__main__":
    main()
