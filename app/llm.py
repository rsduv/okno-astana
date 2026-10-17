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
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import httpx
from dotenv import load_dotenv

from . import geo

load_dotenv()
KEY = os.getenv("OPENROUTER_API_KEY", "")
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_FAST = os.getenv("GEMINI_FAST", "gemini-3.5-flash")   # разбор запроса
GEMINI_MAIN = os.getenv("GEMINI_MAIN", "gemini-3.5-flash")   # тексты «почему»
# без «размышлений» модели отвечают в разы быстрее; не все модели принимают "none"
EFFORT = {"gemini-3.5-flash-lite": "low", "gemini-3.1-flash-lite": "low"}  # ключ Google AI Studio: если есть — идём к Gemini напрямую, OpenRouter — запасной
MODEL = os.getenv("LLM_MODEL", "google/gemini-2.5-flash")
PREFS = ["coffee", "food", "sights", "nature", "culture", "shopping", "kids", "night", "work", "active", "cinema", "walk",
         "unusual", "romantic", "view", "quiet", "indoor"]


FAST = "google/gemini-2.5-flash-lite"


_pool = ThreadPoolExecutor(max_workers=4)


DAILY_CALLS = int(os.getenv("LLM_DAILY_CALLS", "1500"))  # предохранитель бюджета: ~$0,001 за вызов
_calls = {"day": "", "n": 0}


def _allow() -> bool:
    day = time.strftime("%Y-%m-%d")
    if _calls["day"] != day:
        _calls.update(day=day, n=0)
    _calls["n"] += 1
    return _calls["n"] <= DAILY_CALLS


def _chat(system: str, user: str, max_tokens=900, temperature=0.2, fast=False, budget=11.0) -> dict | None:
    """Ответ ИИ с жёстким лимитом времени: если модель тормозит — пробуем вторую, потом шаблон.
    На демо важнее ответить за секунды, чем дождаться идеального текста."""
    if not (KEY or GEMINI_KEY) or not _allow():
        return None
    deadline = time.time() + budget
    for model in ((FAST, MODEL) if fast else (MODEL, FAST)):
        left = deadline - time.time()
        if left < 1.5:
            break
        fut = _pool.submit(_chat_once, model, system, user, max_tokens, temperature)
        try:
            d = fut.result(timeout=left if model == FAST else min(left, budget * 0.6))
        except Exception:
            d = None
        if d is not None:
            return d
    return None


def _chat_once(model, system, user, max_tokens, temperature) -> dict | None:
    if GEMINI_KEY:  # напрямую в Google (OpenAI-совместимый адрес Gemini API)
        gm = GEMINI_FAST if model == FAST else GEMINI_MAIN
        d = _post("https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", GEMINI_KEY,
                  gm, system, user, max_tokens, temperature)
        if d is not None or not KEY:
            return d
    return _post("https://openrouter.ai/api/v1/chat/completions", KEY, model, system, user, max_tokens, temperature)


def _post(url, key, model, system, user, max_tokens, temperature) -> dict | None:
    try:
        body = {"model": model, "temperature": temperature, "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if "googleapis" in url:
            body["reasoning_effort"] = EFFORT.get(model, "none")
        r = httpx.post(url, timeout=14, headers={"Authorization": f"Bearer {key}", "X-Title": "Okno Astana"}, json=body)
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
 "language": "ru" | "kk" | "en" — язык запроса,
 "event": "concert" | "match" | "theatre" | "cinema" | "meeting" | null — если человек идёт на событие (концерт, матч, спектакль, сеанс, встреча),
 "temp_c": число °C, если человек сам назвал погоду («сейчас −30», «мороз 25») — иначе null}
Подсказки: «до вылета/рейса» = destination «аэропорт»; «до поезда» = destination «вокзал Нурлы Жол»;
«кофе» -> coffee; «поесть» -> food; «погулять, прогуляться» -> walk; «посмотреть город, турист» -> sights;
«посидеть, отдохнуть, передохнуть» -> coffee (а не walk); «ночью, тусоваться, бар» -> night; «поработать, розетка, ноутбук» -> work; «с ребёнком/детьми» -> kids.
«необычное, удивить, что-то новое» -> unusual; «романтично, свидание, с девушкой/парнем» -> romantic;
«вид, панорама, посмотреть на город сверху» -> view; «тихо, спокойно, без шума» -> quiet.
«в помещение, внутри, под крышей, на улице холодно/дождь» -> indoor; «поужинать, ужин» -> food.
«Прилетел, рейс дальше в 14:00, хочу в город» = origin «аэропорт», destination «аэропорт», deadline "14:00" (рейс), event null.
«на час» = 60, «полтора часа» = 90. «Через 30 минут надо быть в X» = destination X и duration_min 30 (deadline null).
«До 7 вечера» = deadline "19:00". «Концерт в Барыс Арене в 20:00» = destination «Барыс Арена», deadline "20:00", event "concert". Сейчас %s. Известные ориентиры: %s.
%s
Незнакомые сокращения и названия («мну», «ену», «тётя Гуля») НЕ заменяй на ориентиры из списка — пиши как есть."""


def understand(text: str, now: datetime, has_point: bool = False, saved: list[str] | None = None) -> dict:
    try:
        return _understand(text, now, has_point, saved)
    except Exception:
        d = _rules(text)  # что бы ни вернула модель — запрос всё равно будет понят правилами
        d["prefs"], d["llm"] = [p for p in d["prefs"] if p in PREFS], False
        return d


def _understand(text: str, now: datetime, has_point: bool = False, saved: list[str] | None = None) -> dict:
    names = ", ".join(lm["name"] for lm in geo.LANDMARKS)
    ctx = ("Где пользователь сейчас — уже известно по карте/геолокации: если в фразе названо одно место («в мну универ», "
           "«к тёте Гуле», «домой») — это destination, origin = null, return_to_origin = false." if has_point else "")
    if saved:
        ctx += f" Сохранённые места пользователя: {', '.join(saved)} — используй эти названия как есть."
    d = _chat(UNDERSTAND % (PREFS, now.strftime("%H:%M"), names, ctx), text, max_tokens=300, fast=True, budget=8) or _rules(text)
    if has_point and not d.get("destination") and d.get("origin") and d.get("rules"):
        d["destination"], d["origin"], d["return_to_origin"] = d["origin"], None, False
    d["prefs"] = [p for p in (d.get("prefs") or []) if p in PREFS]
    d["llm"] = "rules" not in d
    return d


def _rules(text: str) -> dict:
    """Запасной разбор без ИИ (если сеть/ключ недоступны)."""
    t = text.lower()
    d = {"origin": None, "destination": None, "return_to_origin": False, "deadline": None, "duration_min": None,
         "prefs": [], "with_kids": "ребён" in t or "дет" in t, "group_size": None, "mobility": "normal",
         "transport": "any", "language": "en" if re.search(r"[a-z]{4}", t) and not re.search(r"[а-я]", t)
         else "kk" if re.search(r"[әіңғүұқөһ]", t) else "ru",
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
    elif re.search(r"полтора|полчаса|пол часа|пар[уы] час|два часа|три часа", t) and not d["duration_min"]:
        d["duration_min"] = 90 if "полтора" in t else 30 if re.search(r"полчаса|пол часа", t) else 180 if "три" in t else 120
    elif ("час" in t or "hour" in t) and not d["duration_min"]:
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
- "why": 1 предложение до 100 символов — почему это место хорошо именно для ЭТОГО человека сейчас
  (символ города / тёплая точка в мороз / то, что он просил; «крюк N мин» — ТОЛЬКО если в фактах есть поле «по_пути»).
  Часы работы НЕ пиши — интерфейс показывает их отдельной строкой. Не начинай с «Это». Не пиши банальностей
  вроде «крюк 0 минут». У разных мест — разные формулировки, без общего шаблона.
  Плохо: «Это известное место города, где можно согреться». Хорошо: «Большой тёплый ТРЦ на пути — переждать ветер».
  Плохо: «Это известная мечеть, всего 5 минут крюка». Хорошо: «Знаковая мечеть города, крюк всего 5 минут».
- "todo": 1 предложение до 90 символов, начинается с глагола — конкретное действие на отведённые минуты.
И "headline": одна фраза до 100 символов — суть окна (например, «Два тёплых места по пути и Хан Шатыр к 18:50»).
ЖЁСТКИЕ ПРАВИЛА честности:
- «Рядом», «в двух шагах», «прямо у» — только если «как_добраться» пешком до 10 мин; если на такси — так и скажи или промолчи.
- Используй только факты из входа. Не придумывай рейтинги, меню, цены, события, историю, вид из окна.
- Если человек просил то, чего нет в фактах (розетки, тишина, Wi-Fi, детская комната, вид), прямо скажи,
  что данных об этом нет, и предложи проверить на месте (например, «розеток в данных нет — спроси у бариста»).
- Про тепло/«согреться»/мороз пиши ТОЛЬКО если погода.ощущается ≤ −5 или погода.симуляция = true.
- Отметки 2GIS упоминай, только если они отвечают на просьбу человека (работа → ноутбук/Wi-Fi; дети → детское;
  иностранец → меню на английском; коляска/пожилой → пандус/доступность). Не вставляй Wi-Fi везде подряд.
- Если в «человек» есть коляска/трудно ходить: скажи, есть ли данные о доступности (wheelchair в OSM или отметки 2GIS);
  нет данных — так и скажи («данных о пандусе нет — уточни заранее»).
- «википедия» — проверенный факт о месте: можно взять ОДНУ конкретную деталь (высота, год, чем знаменито), сказав «по Википедии».
- «рейтинг_2гис» и «отметки_2гис» — настоящие данные 2GIS: можно опираться, называя источник («в 2GIS отмечено: можно с ноутбуком»,
  «4,7 в 2GIS»). Если в отметках есть «Можно с ноутбуком» — это ответ на «розетка/поработать», но про сами розетки всё равно честно: уточнить на месте.
- Если «надёжность_часов» = estimate — не говори «точно открыто», говори «обычно открыто до …».
Язык ответа: %s. Тон — дружелюбный, коротко, без восклицательных знаков и эмодзи.
Верни ТОЛЬКО JSON: {"headline": "...", "stops": [{"id": "...", "why": "...", "todo": "..."}]%s}"""


def explain(request_text: str, intent: dict, result: dict, w: dict) -> dict:
    stops = [s for s in result["steps"] if s["type"] == "stop"]
    reach, prev = {}, None
    for st in result["steps"]:  # как добираться до каждой остановки — чтобы ИИ не писал «рядом» про 10 км на такси
        if st["type"] == "leg":
            prev = st
        elif st["type"] == "stop" and prev:
            reach[st["id"]] = (f"пешком {prev['min']} мин" if prev["mode"] == "walk" else f"на такси ~{prev['min']} мин")
    lang = {"en": "английский", "kk": "казахский"}.get(intent.get("language"), "русский")
    tr = "" if lang == "русский" else ', "notes": ["заметки_алгоритма, переведённые на язык ответа, в том же порядке"]'
    facts = {
        "запрос": request_text,
        "окно_мин": result["summary"]["window_min"], "прибытие": result["summary"]["arrive"],
        "запас_мин": result["summary"]["buffer_min"],
        "погода": {"ощущается": w.get("apparent"), "ветер_кмч": w.get("wind"), "симуляция": w.get("simulated")},
        "заметки_алгоритма": result.get("notes"),
        "человек": {k: v for k, v in {"с_детьми": intent.get("with_kids"),
                                       "коляска_или_трудно_ходить": intent.get("mobility") == "limited",
                                       "размер_группы": intent.get("group_size")}.items() if v},
        "места": [{"id": s["id"], "название": s["name"], "категория": s["category"], "время_там_мин": s["stay"],
                   "приход": s["arrive"], "часы": s["hours"]["text"], "надёжность_часов": s["hours"]["confidence"],
                   "как_добраться": reach.get(s["id"]), "доп_факты": s["facts"]} for s in stops],
    }
    d = _chat(EXPLAIN % (lang, tr), json.dumps(facts, ensure_ascii=False), max_tokens=1200, temperature=0.4)
    if isinstance(d, list):  # модель иногда отдаёт сразу список мест
        d = {"stops": d}
    if not isinstance(d, dict):
        d = None
    if tr and isinstance((d or {}).get("notes"), list) and len(d["notes"]) == len(result.get("notes") or []):
        result["notes_original"] = result["notes"]
        result["notes"] = [str(x) for x in d["notes"]]
    by_id = {x.get("id"): x for x in ((d or {}).get("stops") or []) if isinstance(x, dict)}
    for s in stops:
        x = by_id.get(s["id"]) or {}
        s["why"] = x.get("why") or _why_fallback(s, intent.get("language"))
        s["todo"] = x.get("todo") or _todo_fallback(s, intent.get("language"))
        s["text_source"] = "ai" if x else "template"
    result["headline"] = (d or {}).get("headline") or _headline_fallback(stops, result["summary"]["arrive"], intent.get("language"))
    return result


# Запасные тексты, если ИИ недоступен (нет сети/ключа): факты из данных + действие по типу места, на языке запроса.
TODO = {  # категория: (ru, en, kk)
    "кафе": ("Возьми кофе и передохни", "Grab a coffee and take a break", "Кофе алып, демал"),
    "ресторан": ("Поешь не торопясь", "Have an unhurried meal", "Асықпай тамақтан"),
    "фастфуд": ("Быстро перекуси", "Grab a quick bite", "Жылдам тамақтан"),
    "бар": ("Посиди за напитком", "Have a drink", "Сусын іш, отыр"),
    "торговый центр": ("Пройдись по галереям в тепле", "Walk the indoor galleries", "Жылы галереяларды арала"),
    "музей": ("Посмотри главные залы", "See the main halls", "Басты залдарды көр"),
    "галерея": ("Посмотри текущую выставку", "See the current exhibition", "Қазіргі көрмені көр"),
    "театр": ("Узнай афишу на сегодня", "Check today's program", "Бүгінгі афишаны біл"),
    "кино": ("Выбери сеанс по времени", "Pick a showtime that fits", "Уақытқа сай сеанс таңда"),
    "мечеть": ("Зайди посмотреть интерьер (вход свободный, скромная одежда)", "Step in to see the interior (modest dress)",
               "Ішіне кіріп көр (қарапайым киім)"),
    "собор": ("Зайди посмотреть интерьер", "Step in to see the interior", "Ішін көр"),
    "парк": ("Пройдись по аллеям", "Stroll along the alleys", "Аллеялармен серуенде"),
    "сад": ("Пройдись по саду", "Walk through the garden", "Бақты арала"),
    "детская площадка": ("Дай ребёнку поиграть", "Let the kids play", "Балаға ойнауға мүмкіндік бер"),
    "развлечения": ("Попробуй аттракцион", "Try the attraction", "Аттракционды байқап көр"),
    "библиотека": ("Почитай в тишине", "Read in a quiet spot", "Тыныш жерде кітап оқы"),
    "книжный": ("Полистай новинки", "Browse new books", "Жаңа кітаптарды қара"),
}
DEFAULT_TODO = ("Осмотрись и сделай фото", "Look around and take photos", "Айналаны көріп, суретке түс")
LANG_IX = {"en": 1, "kk": 2}


def _why_fallback(s, language=None):
    i = LANG_IX.get(language or "", 0)
    f = s.get("facts") or {}
    parts = []
    if f.get("википедия") and i == 0:
        parts.append(f["википедия"].split(". ")[0].rstrip(".") + " (Википедия)")
    if f.get("рейтинг_2гис"):
        parts.append(("рейтинг " if i == 0 else "rating " if i == 1 else "рейтинг ") + f["рейтинг_2гис"].split(" ")[0] + " в 2GIS")
    if f.get("по_пути"):
        parts.append({0: f["по_пути"], 1: "right on your way", 2: "жолыңызда"}[i])
    if f.get("внутри"):
        parts.append({0: f["внутри"], 1: "inside the mall", 2: "СОО ішінде"}[i])
    if not parts:
        parts.append({0: f"{s['category'].capitalize()} по пути", 1: "On your way", 2: "Жолыңызда"}[i])
    txt = "; ".join(parts)
    return (txt[:1].upper() + txt[1:])[:160] + ("." if not txt.endswith(".") else "")


def _todo_fallback(s, language=None):
    i = LANG_IX.get(language or "", 0)
    act = TODO.get(s["category"], DEFAULT_TODO)[i]
    return {0: f"{act} — около {s['stay']} мин.", 1: f"{act} — about {s['stay']} min.", 2: f"{act} — шамамен {s['stay']} мин."}[i]


def _headline_fallback(stops, arrive, language=None):
    i = LANG_IX.get(language or "", 0)
    names = ", ".join(x["name"] for x in stops[:3])
    return {0: f"{names} — на месте в {arrive}", 1: f"{names} — arrive by {arrive}", 2: f"{names} — {arrive}-де орнында"}[i]


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


def translate(texts: list[str], language: str | None) -> list[str]:
    """Сообщения системы (отказы, советы) — на язык запроса. Не вышло — оставляем по-русски."""
    lang = {"en": "английский", "kk": "казахский"}.get(language or "")
    if not lang or not texts:
        return texts
    d = _chat(f"Переведи каждую строку на {lang} язык, коротко и дружелюбно, сохраняя числа и названия мест; название сервиса «Окно» пиши как Okno. "
              'Верни ТОЛЬКО JSON: {"t": ["...", "..."]} в том же порядке.',
              json.dumps(texts, ensure_ascii=False), max_tokens=600, fast=True, budget=6)
    out = (d or {}).get("t")
    return [str(x) for x in out] if isinstance(out, list) and len(out) == len(texts) else texts

