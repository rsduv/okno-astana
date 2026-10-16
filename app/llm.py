"""ИИ-часть: Gemini через OpenRouter.

1) understand(): свободная фраза -> структурированный запрос (откуда, куда, к скольки, что хочется).
2) explain(): факты выбранных мест -> короткие «почему» и «что делать» на языке пользователя.
ИИ не выбирает места и не придумывает факты — только понимает запрос и объясняет решение планировщика.
Если ИИ недоступен, работают простые правила, чтобы демо не падало.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta

import httpx
from dotenv import load_dotenv

from . import geo

load_dotenv()
KEY = os.getenv("OPENROUTER_API_KEY", "")
MODEL = os.getenv("LLM_MODEL", "google/gemini-2.5-flash")
PREFS = ["coffee", "food", "sights", "nature", "culture", "shopping", "kids", "night", "work", "active", "cinema", "walk"]


FAST = "google/gemini-2.5-flash-lite"


def _chat(system: str, user: str, max_tokens=900, temperature=0.2, fast=False) -> dict | None:
    if not KEY:
        return None
    for model in ((FAST, MODEL) if fast else (MODEL, FAST)):  # вторая попытка — более быстрая модель
        d = _chat_once(model, system, user, max_tokens, temperature)
        if d is not None:
            return d
    return None


def _chat_once(model, system, user, max_tokens, temperature) -> dict | None:
    try:
        r = httpx.post("https://openrouter.ai/api/v1/chat/completions", timeout=14, headers={
            "Authorization": f"Bearer {KEY}", "X-Title": "Okno Astana"}, json={
            "model": model, "temperature": temperature, "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
        txt = r.json()["choices"][0]["message"]["content"]
        txt = re.sub(r"^```(?:json)?|```$", "", txt.strip(), flags=re.M).strip()
        return json.loads(txt)
    except Exception:
        return None


UNDERSTAND = """Ты разбираешь запрос жителя или гостя Астаны о свободном времени. Верни ТОЛЬКО JSON:
{"origin": "название места, где человек сейчас, как он написал, или null",
 "destination": "куда нужно прийти в конце, или null если не сказано",
 "return_to_origin": true/false (true если «потом обратно», «вернуться сюда», или нет пункта назначения),
 "deadline": "HH:MM к которому нужно быть в конечной точке, или null",
 "duration_min": число минут свободного времени или null,
 "prefs": подмножество %s,
 "with_kids": true/false, "group_size": число или null,
 "mobility": "limited" если коляска/трудно ходить/пожилой, иначе "normal",
 "transport": "walk" если человек явно хочет только пешком, иначе "any",
 "language": "ru" | "kk" | "en" — язык запроса}
Подсказки: «до вылета/рейса» = destination «аэропорт»; «до поезда» = destination «вокзал Нурлы Жол»;
«кофе» -> coffee; «поесть» -> food; «погулять, прогуляться» -> walk; «посмотреть город, турист» -> sights;
«ночью, тусоваться, бар» -> night; «поработать, розетка, ноутбук» -> work; «с ребёнком/детьми» -> kids.
«на час» = 60, «полтора часа» = 90. Сейчас %s. Известные ориентиры: %s."""


def understand(text: str, now: datetime) -> dict:
    names = ", ".join(lm["name"] for lm in geo.LANDMARKS)
    d = _chat(UNDERSTAND % (PREFS, now.strftime("%H:%M"), names), text, max_tokens=300, fast=True) or _rules(text)
    d["prefs"] = [p for p in (d.get("prefs") or []) if p in PREFS]
    d["llm"] = "rules" not in d
    return d


def _rules(text: str) -> dict:
    """Запасной разбор без ИИ (если сеть/ключ недоступны)."""
    t = text.lower()
    d = {"origin": None, "destination": None, "return_to_origin": False, "deadline": None, "duration_min": None,
         "prefs": [], "with_kids": "ребён" in t or "дет" in t, "group_size": None, "mobility": "normal",
         "transport": "any", "language": "en" if re.search(r"[a-z]{4}", t) and not re.search(r"[а-я]", t) else "ru",
         "rules": True}
    m = re.search(r"(?:к|до|by)\s*(\d{1,2})[:.](\d{2})", t) or re.search(r"к\s*(\d{1,2})\b(?!\s*мин)", t)
    if m:
        h = int(m.group(1)); mm = int(m.group(2)) if m.lastindex and m.lastindex >= 2 else 0
        d["deadline"] = f"{h:02d}:{mm:02d}"
    m = re.search(r"(\d+)\s*(?:мин|min)", t)
    if m:
        d["duration_min"] = int(m.group(1))
    m = re.search(r"(\d+(?:[.,]\d)?)\s*(?:час|ч\b|hour|h\b)", t)
    if m:
        d["duration_min"] = int(float(m.group(1).replace(",", ".")) * 60)
    elif "час" in t and not d["duration_min"]:
        d["duration_min"] = 60
    parts = re.split(r"\s(?:в|до|к|потом в|надо быть в|нужно в|to)\s", t, maxsplit=1)
    lm_found = []
    for lm in geo.LANDMARKS:
        for al in lm["aliases"]:
            i = t.find(al)
            if i >= 0:
                lm_found.append((i, lm["name"]))
                break
    lm_found.sort()
    if lm_found:
        d["origin"] = lm_found[0][1]
        if len(lm_found) > 1:
            d["destination"] = lm_found[-1][1]
    if "вылет" in t or "рейс" in t or "flight" in t:
        d["destination"] = "аэропорт"
    for k, words in {"coffee": ["кофе", "coffee"], "food": ["поесть", "обед", "ужин", "food"], "walk": ["погул", "прогул", "walk"],
                     "sights": ["посмотреть", "турист", "sight"], "night": ["ноч", "бар", "night"], "work": ["розетк", "поработ", "ноут"],
                     "kids": ["ребён", "дет", "kid"]}.items():
        if any(w in t for w in words):
            d["prefs"].append(k)
    if not d["destination"]:
        d["return_to_origin"] = True
    return d


EXPLAIN = """Ты — «Окно», городской помощник по Астане. Алгоритм УЖЕ выбрал маршрут; тебе даны ФАКТЫ о каждом месте.
Напиши для каждого места:
- "why": 1 предложение до 120 символов — почему это место хорошо именно для ЭТОГО человека сейчас.
  Сначала — чем место ценно (известное место города / тёплая точка в мороз / по пути без крюка / то, что он просил),
  потом практическая деталь (крюк N мин, открыто до HH:MM). Не пиши банальностей вроде «часы работы не важны».
- "todo": 1 предложение до 110 символов — конкретное действие на отведённые минуты, здравый смысл для категории.
И "headline": одна фраза до 100 символов — суть окна (например, «Два тёплых места по пути и Хан Шатыр к 18:50»).
ЖЁСТКИЕ ПРАВИЛА честности:
- Используй только факты из входа. Не придумывай рейтинги, меню, цены, события, историю, вид из окна.
- Если человек просил то, чего нет в фактах (розетки, тишина, Wi-Fi, детская комната, вид), прямо скажи,
  что данных об этом нет, и предложи проверить на месте (например, «розеток в данных нет — спроси у бариста»).
- Если «надёжность_часов» = estimate — не говори «точно открыто», говори «обычно открыто до …».
Язык ответа: %s. Тон — дружелюбный, коротко, без восклицательных знаков и эмодзи.
Верни ТОЛЬКО JSON: {"headline": "...", "stops": [{"id": "...", "why": "...", "todo": "..."}]}"""


def explain(request_text: str, intent: dict, result: dict, w: dict) -> dict:
    stops = [s for s in result["steps"] if s["type"] == "stop"]
    lang = {"en": "английский", "kk": "казахский"}.get(intent.get("language"), "русский")
    facts = {
        "запрос": request_text,
        "окно_мин": result["summary"]["window_min"], "прибытие": result["summary"]["arrive"],
        "запас_мин": result["summary"]["buffer_min"],
        "погода": {"ощущается": w.get("apparent"), "ветер_кмч": w.get("wind"), "симуляция": w.get("simulated")},
        "заметки_алгоритма": result.get("notes"),
        "места": [{"id": s["id"], "название": s["name"], "категория": s["category"], "время_там_мин": s["stay"],
                   "приход": s["arrive"], "часы": s["hours"]["text"], "надёжность_часов": s["hours"]["confidence"],
                   "доп_факты": s["facts"]} for s in stops],
    }
    d = _chat(EXPLAIN % lang, json.dumps(facts, ensure_ascii=False), max_tokens=900, temperature=0.4)
    by_id = {x.get("id"): x for x in (d or {}).get("stops", [])}
    for s in stops:
        x = by_id.get(s["id"]) or {}
        s["why"] = x.get("why") or _why_fallback(s)
        s["todo"] = x.get("todo") or _todo_fallback(s)
        s["text_source"] = "ai" if x else "template"
    word = {1: "остановка", 2: "остановки", 3: "остановки"}.get(len(stops), "остановок")
    result["headline"] = (d or {}).get("headline") or f"{len(stops)} {word} по пути, на месте в {result['summary']['arrive']}"
    return result


def _why_fallback(s):
    h = s["hours"]
    base = {"verified": "часы работы проверены по данным", "estimate": "часы — оценка",
            "outdoor": "открытое место"}.get(h["confidence"], "")
    return f"{s['category'].capitalize()} по пути, {base}."


def _todo_fallback(s):
    return f"Около {s['stay']} минут: {'кофе и передышка' if s['category'] == 'кафе' else 'осмотреться и сделать фото'}."


def resolve_times(parsed: dict, now: datetime) -> tuple[datetime, str]:
    """deadline из «к 19:00» или «на 45 минут»; по умолчанию — 90 минут."""
    dl = parsed.get("deadline")
    if dl:
        h, m = map(int, dl.split(":"))
        t = now.replace(hour=h % 24, minute=m, second=0, microsecond=0)
        if t <= now:
            t += timedelta(days=1)
        return t, f"к {dl}"
    mins = parsed.get("duration_min") or 90
    return now + timedelta(minutes=mins), f"{mins} мин"
