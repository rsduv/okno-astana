"""Городской каркас из OpenStreetMap: районы Астаны (границы) и станции LRT — для колонки «район» в датасете
и для будущего учёта LRT в маршрутах.

    python scripts/collect_city.py
Результат: data/districts.json, data/lrt.json. Только httpx.
"""
import json
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
UA = {"User-Agent": "OknoAstana/0.1 (https://github.com/rsduv/okno-astana)"}
ENDPOINTS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]
BBOX = "51.03,71.20,51.28,71.65"
DISTRICTS = {"Есіл ауданы": "Есиль", "Алматы ауданы": "Алматы", "Сарыарқа ауданы": "Сарыарка",
             "Байқоңыр ауданы": "Байконур", "Нұра ауданы": "Нура", "Сарайшық ауданы": "Сарайшык"}


def overpass(q: str) -> list:
    for u in ENDPOINTS:
        try:
            r = httpx.post(u, data={"data": q}, timeout=120, headers=UA)
            if r.status_code == 200:
                return r.json()["elements"]
        except httpx.HTTPError:
            pass
    raise SystemExit("Overpass не ответил")


def districts():
    els = overpass(f'[out:json][timeout:90];rel[boundary=administrative][admin_level=6]({BBOX});out geom;')
    out = []
    for e in els:
        name = e["tags"].get("name")
        if name not in DISTRICTS:
            continue
        # отрезки внешних границ: для «точка внутри?» хватает чётности пересечений, склеивать кольца не нужно
        segs = []
        for m in e.get("members", []):
            if m.get("role") == "outer" and m.get("geometry"):
                g = [(round(p["lat"], 5), round(p["lon"], 5)) for p in m["geometry"]]
                segs += [[*a, *b] for a, b in zip(g, g[1:])]
        out.append({"name": DISTRICTS[name], "name_kk": name, "osm": f"relation/{e['id']}", "segments": segs})
    return out


def lrt():
    els = overpass(f'[out:json][timeout:60];(node[station=light_rail]({BBOX});node[railway=station][light_rail=yes]({BBOX});'
                   f'rel[route=light_rail]({BBOX}););out body;')
    stations = {e["id"]: e for e in els if e["type"] == "node"}
    routes = [e for e in els if e["type"] == "relation"]
    order = []
    if routes:
        r = max(routes, key=lambda x: len(x.get("members", [])))
        order = [m["ref"] for m in r["members"] if m["type"] == "node" and m["ref"] in stations and m.get("role", "").startswith("stop")]
        order = order or [m["ref"] for m in r["members"] if m["type"] == "node" and m["ref"] in stations]
    order += [i for i in stations if i not in order]
    seen, res = set(), []
    for i in order:
        t = stations[i]["tags"]
        name = t.get("name:ru") or t.get("name")
        if not name or name in seen:
            continue
        seen.add(name)
        res.append({"order": len(res) + 1, "name": name, "name_kk": t.get("name:kk") or t.get("name"), "name_en": t.get("name:en"),
                    "lat": stations[i]["lat"], "lon": stations[i]["lon"], "osm": f"node/{i}"})
    return {"line": routes[0]["tags"].get("name") if routes else None, "stations": res}


def inside(lat, lon, segs):
    c = False
    for a_lat, a_lon, b_lat, b_lon in segs:
        if (a_lat > lat) != (b_lat > lat) and lon < (b_lon - a_lon) * (lat - a_lat) / (b_lat - a_lat) + a_lon:
            c = not c
    return c


def main():
    d = districts()
    (ROOT / "data" / "districts.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    print("районов:", len(d), [x["name"] for x in d])
    lr = lrt()
    (ROOT / "data" / "lrt.json").write_text(json.dumps(lr, ensure_ascii=False, indent=1), encoding="utf-8")
    print("LRT:", lr["line"], len(lr["stations"]), "станций:", ", ".join(s["name"] for s in lr["stations"]))


if __name__ == "__main__":
    main()
