"""Афиша Астаны — автоматический сбор событий → data/events.json.

Источник: официальный сайт ASTANA OPERA (astanaopera.kz, robots.txt разрешает), открытый JSON, который использует сама их афиша.
Берём только факты: название, дата и время начала, длительность, зал, жанр, возраст, ссылку на страницу события.
Запуск: python scripts/collect_events.py   (GitHub Actions — каждый день)
"""
import json
import re
import time
from datetime import date
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "events.json"
UA = {"User-Agent": "OknoAstana/0.1 (https://github.com/rsduv/okno-astana)"}
OPERA = {"venue": "Астана Опера", "lat": 51.13546, "lon": 71.410659}


def opera() -> list[dict]:
    out, page, last = [], 1, 1
    while page <= last and page <= 10:
        r = httpx.get("https://astanaopera.kz/api/posters", headers=UA, timeout=30,
                      params={"page": page, "lang": "ru", "limit": 50}).json()
        last = r["data"]["meta"].get("last_page") or 1
        for it in r["data"]["data"]:
            ev = it.get("event") or {}
            if not ev.get("event_date") or not ev.get("event_time"):
                continue
            out.append({
                "id": f"opera/{it['id']}/{ev['event_date']}",
                "title": re.sub(r"\s+", " ", it["title"]).strip(),
                "date": ev["event_date"], "time": ev["event_time"][:5],
                "minutes": int(it.get("minutes") or 120),
                "hall": ev.get("hall_name") or it.get("scene_name"),
                "kind": it.get("repertoire_name"), "age": it.get("age_limit"),
                "url": f"https://astanaopera.kz/afisha/{it['id']}",
                "source": "astanaopera.kz", **OPERA,
            })
        page += 1
        time.sleep(0.5)
    return out


def main():
    events = []
    try:
        events += opera()
    except Exception as e:
        print("Astana Opera: не получилось —", e)
    today = date.today().isoformat()
    events = sorted((e for e in events if e["date"] >= today), key=lambda e: (e["date"], e["time"]))
    if not events and OUT.exists():
        print("новых событий нет — оставляю прежний файл")
        return
    OUT.write_text(json.dumps({"updated": today, "events": events}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"готово: {len(events)} событий → {OUT}")


if __name__ == "__main__":
    main()
