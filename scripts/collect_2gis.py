"""Сбор популярных мест Астаны из 2GIS Places API (демо-ключ): python scripts/collect_2gis.py

Демо-ключ отдаёт максимум 10 мест на страницу и 5 страниц на запрос, поэтому идём по районам и категориям,
только места с рейтингом. Результат — data/dgis_pois.json. Файл НЕ публикуется (данные 2GIS — не наши),
он лишь дополняет открытую базу OSM у того, кто запускает систему со своим ключом.
"""
import json
import os
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
KEY = os.getenv("DGIS_KEY")
OUT = ROOT / "data" / "dgis_pois.json"
URL = "https://catalog.api.2gis.com/3.0/items"
FIELDS = "items.point,items.reviews,items.schedule,items.attribute_groups,items.rubrics"

# (запрос в 2GIS, наша категория)
CATS = [("кофейня", "кафе"), ("ресторан", "ресторан"), ("бар", "бар"), ("музей", "музей"),
        ("художественная галерея", "галерея"), ("детский развлекательный центр", "развлечения"),
        ("кинотеатр", "кино"), ("коворкинг", "коворкинг")]
# центры районов: левый берег, ЭКСПО/Мега, правый берег (старый город), Хан Шатыр/запад, восток
AREAS = [(51.128, 71.430), (51.092, 71.412), (51.165, 71.440), (51.140, 71.400), (51.150, 71.475)]
PAGES = int(sys.argv[1]) if len(sys.argv) > 1 else 2


def main():
    if not KEY:
        sys.exit("нет DGIS_KEY в .env")
    found = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    n = 0
    for q, cat in CATS:
        for lat, lon in AREAS:
            for page in range(1, PAGES + 1):
                r = httpx.get(URL, timeout=10, params={"q": q, "point": f"{lon},{lat}", "radius": 3500, "page_size": 10,
                                                       "page": page, "fields": FIELDS,
                                                       "locale": "ru_KZ", "key": KEY}).json()
                n += 1
                code = r.get("meta", {}).get("code")
                if code != 200:
                    print("стоп:", code, r.get("meta", {}).get("error", {}).get("message"))
                    if code in (403, 429):
                        OUT.write_text(json.dumps(found, ensure_ascii=False), encoding="utf-8")
                        return
                    break
                items = r.get("result", {}).get("items", [])
                for it in items:
                    pt = it.get("point") or {}
                    if "lat" not in pt or it.get("type") != "branch":
                        continue
                    found[it["id"]] = {"id": it["id"], "name": it.get("name", ""), "category": cat, "lat": pt["lat"],
                                       "lon": pt["lon"], "address": it.get("address_name"), "reviews": it.get("reviews"),
                                       "schedule": it.get("schedule"), "attribute_groups": it.get("attribute_groups"),
                                       "rubrics": [x.get("name") for x in it.get("rubrics") or []]}
                print(f"{q:30} {lat},{lon} p{page}: +{len(items)}  всего {len(found)}")
                if len(items) < 10:
                    break
                time.sleep(3.2)  # демо-ключ: держим ~18 запросов в минуту
            time.sleep(3.2)
    OUT.write_text(json.dumps(found, ensure_ascii=False), encoding="utf-8")
    print(f"готово: {len(found)} мест, запросов {n}")


if __name__ == "__main__":
    main()
