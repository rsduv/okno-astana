"""Справочник ориентиров Астаны (для понимания фраз «я у Байтерека»). Геокодер — Nominatim (OSM), 1 запрос/сек."""
import json, time, urllib.parse, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# ключ: (запрос в Nominatim, синонимы для распознавания)
LANDMARKS = {
    "ЭКСПО": ("Nur Alem, Astana", ["экспо", "expo", "нур алем", "nur alem", "шар", "aitu", "аиту", "мангилик ел 55"]),
    "Mega Silk Way": ("Mega Silk Way, Astana", ["mega silk way", "мега силк вей", "мега", "mega"]),
    "Хан Шатыр": ("Khan Shatyr, Astana", ["хан шатыр", "khan shatyr", "шатыр"]),
    "Байтерек": ("Bayterek, Astana", ["байтерек", "bayterek", "baiterek"]),
    "Аэропорт Нурсултан Назарбаев": ("Nursultan Nazarbayev International Airport", ["аэропорт", "airport", "nqz", "вылет", "рейс"]),
    "Вокзал Нурлы Жол": ("Nurly Zhol, Astana", ["нурлы жол", "nurly zhol", "новый вокзал", "вокзал"]),
    "Вокзал Астана-1": ("Astana-1 railway station", ["астана-1", "старый вокзал", "astana-1"]),
    "Хазрет Султан": ("Hazrat Sultan Mosque", ["хазрет султан", "hazrat sultan", "мечеть"]),
    "Дворец мира и согласия": ("Palace of Peace and Reconciliation", ["пирамида", "дворец мира", "pyramid"]),
    "Ак Орда": ("Ak Orda Presidential Palace", ["ак орда", "akorda", "ак орде"]),
    "Астана Опера": ("Astana Opera", ["опера", "astana opera", "театр оперы"]),
    "Национальный музей": ("National Museum of the Republic of Kazakhstan", ["национальный музей", "national museum"]),
    "Астана Арена": ("Astana Arena", ["астана арена", "astana arena", "стадион"]),
    "Керуен": ("Keruen, Astana", ["керуен", "keruen"]),
    "Абу Даби Плаза": ("Abu Dhabi Plaza, Astana", ["абу даби", "abu dhabi plaza"]),
    "Центральный парк": ("Central Park, Astana", ["центральный парк", "central park"]),
    "Назарбаев Университет": ("Nazarbayev University", ["назарбаев университет", "nazarbayev university", "nu", "назарбаев уни"]),
    "Площадь Независимости": ("Independence Square, Astana", ["площадь независимости", "казак ели"]),
    "Триумфальная арка": ("Mangilik El Triumphal Arch", ["триумфальная арка", "арка", "мангилик ел арка"]),
    "Сарыарка (велотрек)": ("Saryarka Velodrome", ["сарыарка", "велотрек"]),
}
VIEWBOX = "71.2,51.28,71.65,51.0"

def geocode(q):
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": q, "format": "jsonv2", "limit": 1, "viewbox": VIEWBOX, "bounded": 1})
    req = urllib.request.Request(url, headers={"User-Agent": "okno-astana-hackathon/0.1 (github.com/rsduv)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read().decode())
    return d[0] if d else None

out = []
for name, (q, syn) in LANDMARKS.items():
    d = geocode(q)
    time.sleep(1.1)
    if not d:
        print("НЕ НАЙДЕНО:", name); continue
    out.append({"name": name, "lat": round(float(d["lat"]), 6), "lon": round(float(d["lon"]), 6),
                "aliases": [name.lower()] + syn, "osm": d.get("display_name", "")[:80]})
    print(name, out[-1]["lat"], out[-1]["lon"], "|", out[-1]["osm"])
(ROOT / "data" / "landmarks.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
