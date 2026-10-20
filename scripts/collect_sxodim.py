"""Городская афиша Астаны → data/events_city.json: выставки, концерты, вечеринки, квизы, стендап, детское, матчи.

Источник: sxodim.com — городской гид «куда сходить» (robots.txt разрешает всё). У каждого события на странице есть
разметка schema.org/Event: название, дата и время начала (у выставок — период), место с адресом, цена, ссылка на билеты.
Берём только эти факты; координаты площадки находим по адресу (наша база мест → OpenStreetMap Nominatim).
Запросы — по одному в секунду. Запуск: python scripts/collect_sxodim.py   (GitHub Actions — каждый день)
"""
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

os.environ.setdefault("DGIS_OFF", "1")   # координаты — без 2GIS (у демо-ключа маленький лимит)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import geo, planner  # noqa: E402

OUT = ROOT / "data" / "events_city.json"
UA = {"User-Agent": "Mozilla/5.0 (compatible; OknoAstana/0.1; +https://github.com/rsduv/okno-astana)"}
BASE = "https://sxodim.com/astana"
CATS = ["kontserty", "vystavki", "vecherinki", "igry", "dlya-detey", "teatr", "stand-up", "match-hk-barys", "festivali",
        "besplatnye-sobytiya", "kino", "sport"]
# сколько обычно длится (мин) — по разделу афиши; у выставки — сколько закладываем на посещение
MINUTES = [("выстав", 60), ("квиз", 120), ("игр", 120), ("стендап", 90), ("stand", 90), ("вечерин", 180), ("концерт", 120),
           ("спектак", 120), ("театр", 120), ("мюзикл", 150), ("матч", 150), ("спорт", 120), ("кино", 120), ("дет", 60),
           ("лекци", 90), ("мастер", 90), ("фестив", 180), ("экскурс", 120)]
HORIZON = 30   # дней вперёд


def get(url: str) -> str | None:
    for _ in range(2):
        try:
            r = httpx.get(url, headers=UA, timeout=30, follow_redirects=True)
            if r.status_code == 200:
                return r.text
        except httpx.HTTPError:
            pass
        time.sleep(2)
    return None


def event_links() -> list[str]:
    links: list[str] = []
    pages = [f"{BASE}/afisha?page={i}" for i in range(1, 11)] + [f"{BASE}/events/{c}" for c in CATS]
    for u in pages:
        t = get(u)
        time.sleep(1)
        if not t:
            continue
        for x in re.findall(r"https://sxodim\.com/astana/event/[a-z0-9\-]+", t):
            if x not in links:
                links.append(x)
    return links


def parse(url: str) -> dict | None:
    t = get(url)
    if not t:
        return None
    ev, kind = None, ""
    for m in re.findall(r'<script type="application/ld\+json">(.*?)</script>', t, re.S):
        try:
            d = json.loads(m)
        except ValueError:
            continue
        if d.get("@type") == "Event":
            ev = d
        if d.get("@type") == "BreadcrumbList":
            items = d.get("itemListElement") or []
            if len(items) >= 2:
                kind = items[1].get("name", "")
    if not ev or not ev.get("startDate"):
        return None
    addr = ((ev.get("location") or {}).get("address") or {}).get("streetAddress", "").strip()
    venue = addr.split(",")[0].strip() if addr else ""
    offer = ev.get("offers") or {}
    price = offer.get("price") if isinstance(offer, dict) else None
    return {"title": ev["name"].strip(), "kind": kind, "start": ev["startDate"], "end": ev.get("endDate"),
            "venue": venue, "address": addr, "price": price, "url": url,
            "status": (ev.get("eventStatus") or "").rsplit("/", 1)[-1]}


def minutes_for(kind: str, title: str) -> int:
    k = (kind + " " + title).lower()
    return next((m for key, m in MINUTES if key in k), 120)


GENERIC = re.compile(r"^(г\.?\s*)?астана( қаласы)?$|^онлайн|^online|^по согласованию", re.I)
OTHER_CITY = re.compile(r"алмат|шымкент|караганд|балхаш|балқаш|актобе|ақтөбе|атырау|актау|ақтау|павлодар|семей|костанай|"
                        r"қостанай|кокшетау|көкшетау|тараз|туркестан|түркістан|уральск|орал|петропавл|талдыкорган|жезказган", re.I)
STREET = [(r"\bпр\.?\s*", "проспект "), (r"\bул\.?\s*", "улица "), (r"\bб-р\.?\s*", "бульвар "), (r"\bмкр\.?\s*", "микрорайон "),
          (r"\bш\.?\s*", "шоссе "), (r"\s+", " ")]


def _nominatim(q: str):
    try:
        r = httpx.get("https://nominatim.openstreetmap.org/search", timeout=15,
                      headers={"User-Agent": "OknoAstana/0.1 (https://github.com/rsduv/okno-astana)"},
                      params={"q": q, "format": "jsonv2", "limit": 1, "countrycodes": "kz",
                              "viewbox": "71.20,51.28,71.65,51.00", "bounded": 1, "accept-language": "ru"})
        time.sleep(1.1)   # правило Nominatim: не чаще 1 запроса в секунду
        d = r.json()
        return (float(d[0]["lat"]), float(d[0]["lon"])) if d else None
    except Exception:
        return None


def locate(venue: str, address: str):
    """Площадка → координаты. Точнее всего — улица и дом из адреса (Nominatim, только в пределах Астаны);
    иначе — название площадки, но только если оно нашлось в нашей базе мест или среди ориентиров."""
    parts = re.split(r"[;,]", address, maxsplit=1)
    tail = parts[1].strip() if len(parts) > 1 else ""
    # нужна именно улица с номером дома, кириллицей («ул. Кенесары, 32»); «32» или латиница — слишком неточно для геокодера
    if re.search(r"[А-Яа-яЁёӘәҚқҢңӨөҰұҮүІі]{4,}", tail) and re.search(r"\d", tail):
        q = tail
        for a, b in STREET:
            q = re.sub(a, b, q, flags=re.I)
        loc = _nominatim(f"{q}, Астана")
        if loc:
            return loc
    if venue and not GENERIC.search(venue):
        p = geo.find_place(venue, planner.POIS, {})
        if p and p.get("source") in ("справочник ориентиров", "база мест OSM", "геокодер OpenStreetMap"):
            return p["lat"], p["lon"]
    return None


def main():
    today = date.today()
    links = event_links()
    print("ссылок на события:", len(links), flush=True)
    out, seen, places = [], set(), {}
    for i, u in enumerate(links, 1):
        e = parse(u)
        time.sleep(1)
        if not e or e["status"] in ("EventCancelled", "EventPostponed"):
            continue
        if OTHER_CITY.search(e["title"] + " " + e["address"]) or GENERIC.search(e["venue"] or "") and "," not in e["address"]:
            continue   # гастроли в другом городе или площадка не указана — в маршрут не поставишь
        timed = "T" in e["start"]
        d0 = date.fromisoformat(e["start"][:10])
        d1 = date.fromisoformat(e["end"][:10]) if e.get("end") else d0
        if d1 < today or d0 > today + timedelta(days=HORIZON):
            continue
        key = e["venue"] or e["address"]
        if key not in places:
            places[key] = locate(e["venue"], e["address"])
        loc = places[key]
        if not loc:
            continue
        rec = {"id": "sxodim/" + u.rsplit("/", 1)[-1], "title": e["title"], "kind": e["kind"], "venue": e["venue"],
               "address": e["address"], "price": e["price"], "url": u, "source": "sxodim.com",
               "lat": round(loc[0], 6), "lon": round(loc[1], 6), "minutes": minutes_for(e["kind"], e["title"])}
        if timed:
            t0 = datetime.fromisoformat(e["start"]).replace(tzinfo=None)
            rec |= {"date": t0.strftime("%Y-%m-%d"), "time": t0.strftime("%H:%M")}
        else:   # выставка/фестиваль на период: можно прийти в любой день периода в часы работы
            rec |= {"date": max(d0, today).isoformat(), "until": d1.isoformat(), "time": None, "range": True}
        if rec["id"] in seen:
            continue
        seen.add(rec["id"])
        out.append(rec)
        if i % 20 == 0:
            print(f"  {i}/{len(links)} → {len(out)} событий", flush=True)
    out.sort(key=lambda r: (r["date"], r.get("time") or "00:00"))
    if not out:
        raise SystemExit("ничего не собрано — data/events_city.json не трогаю")
    OUT.write_text(json.dumps({"updated": today.isoformat(), "source": "sxodim.com", "events": out}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    kinds = {}
    for r in out:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    print(f"{len(out)} событий, площадок {sum(1 for v in places.values() if v)}/{len(places)}; {kinds}")


if __name__ == "__main__":
    main()
