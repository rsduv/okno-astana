"""Скриншоты сценариев для проверки и для презентации: python scripts/shots.py [фильтр]"""
import sys, urllib.parse
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SC = {
    "1_expo": ("Я в ЭКСПО, к 19:00 надо быть в Хан Шатыре", "17:20", "now"),
    "2_expo_cold": ("Я в ЭКСПО, к 19:00 надо быть в Хан Шатыре", "17:20", "cold"),
    "3_mega": ("Я у Mega Silk Way, хочу прогуляться минут 45 и вернуться", "14:00", "now"),
    "4_tourist": ("I'm at Baiterek, my flight is at 18:30, want to see the city", "15:00", "now"),
    "5_kids": ("С ребёнком у Керуена, есть полтора часа", "11:00", "now"),
    "6_work": ("Хочу тихое место с кофе и розеткой, я у Абу Даби Плаза, часа два", "10:00", "now"),
    "7_night": ("Ночью у Хан Шатыра, хочу что-то необычное на пару часов", "23:00", "now"),
    "8_event": ("У меня концерт в Барыс Арене в 20:00, я у Байтерека, хочу перекусить по пути", "17:30", "now"),
    "9_kk": ("Хан Шатырдың жанындамын, бір сағат бар, кофе ішкім келеді", "16:00", "now"),
}
flt = sys.argv[1] if len(sys.argv) > 1 else ""
h = int(sys.argv[2]) if len(sys.argv) > 2 else 1000
(ROOT / "shots").mkdir(exist_ok=True)
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True,
                          args=[])
    pg = b.new_page(viewport={"width": 1536, "height": h})
    for name, (q, t, w) in SC.items():
        if flt not in name:
            continue
        pg.goto(f"http://127.0.0.1:8010/?q={urllib.parse.quote(q)}&t={t}&w={w}")
        pg.wait_for_selector(".sum .l1, .sum .err", timeout=90000)
        pg.wait_for_timeout(9000)
        out = ROOT / "shots" / f"{name}.png"
        pg.screenshot(path=str(out))
        print(out)
    b.close()
