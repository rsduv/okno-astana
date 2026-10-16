"""Прогон живых фраз через API: python scripts/try_api.py"""
import json, sys, time
import httpx
PHRASES = [
    ("Я в ЭКСПО, к 19:00 надо быть в Хан Шатыре", "17:20", "now"),
    ("Я в ЭКСПО, к 19:00 надо быть в Хан Шатыре", "17:20", "cold"),
    ("я у мега силк вей, хочу прогуляться минут 45 и вернуться", "14:00", "now"),
    ("I'm at Baiterek, my flight is at 18:30, I have about 2 hours, want to see the city", "15:00", "now"),
    ("с ребёнком у Керуена, есть полтора часа", "11:00", "now"),
    ("хочу тихое место с кофе и розеткой, я у Абу Даби Плаза, часа два", "10:00", "now"),
    ("ночью у Хан Шатыра, хочу что-то необычное на пару часов", "23:00", "now"),
]
sel = int(sys.argv[1]) if len(sys.argv) > 1 else None
for i, (text, now, w) in enumerate(PHRASES):
    if sel is not None and i != sel:
        continue
    t = time.time()
    r = httpx.post("http://127.0.0.1:8000/api/plan", json={"text": text, "now": now, "weather": w}, timeout=120).json()
    print(f"\n### {text}  [{now}, {w}]  {time.time()-t:.1f}c  {r.get('timings')}")
    p = r.get("parsed", {})
    print("  понял:", {k: p.get(k) for k in ("origin", "destination", "deadline", "duration_min", "prefs", "language", "llm")})
    if r.get("error"):
        print("  ОШИБКА:", r["error"]); continue
    print("  ", r["origin"]["label"], "→", r["destination"]["label"], "|", r["window_label"], "|", r["summary"])
    print("   >>", r.get("headline"))
    for s in r["steps"]:
        if s["type"] == "stop":
            print(f"   * {s['arrive']} {s['name']} [{s['category']}] {s['hours']['text']}")
            print(f"       почему: {s['why']}\n       делать: {s['todo']}")
        elif s["type"] == "leg":
            print(f"     {'🚶' if s['mode']=='walk' else '🚕'} {s['min']} мин")
    for n in r.get("notes", []):
        print("   !", n)
