"""Презентация «Окна» (5–7 минут): снимает свежие скриншоты с работающего сервера и собирает слайды в HTML и PDF.

Запуск (сервер на :8010): python scripts/make_slides.py [--no-shots]  →  docs/slides/okno_slides.html и docs/Okno_presentation.pdf
"""
import re
import sys
import urllib.parse
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DIR = ROOT / "docs" / "slides"
IMG = DIR / "img"
URL = "http://127.0.0.1:8010/"


def shots():
    IMG.mkdir(parents=True, exist_ok=True)
    q = lambda t, tm, w="now": f"{URL}?q={urllib.parse.quote(t)}&t={tm}&w={w}"
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True)
        ctx = b.new_context(viewport={"width": 1600, "height": 900}, geolocation={"latitude": 51.0905, "longitude": 71.418},
                            permissions=["geolocation"])
        pg = ctx.new_page()
        pg.add_init_script("try{localStorage.clear()}catch(e){}")

        def go(url, name, after=None, wait=6000):
            pg.goto(url)
            pg.wait_for_selector(".sum .l1, .sum .err", timeout=90000)
            pg.wait_for_timeout(wait)
            if after:
                after()
            pg.screenshot(path=str(IMG / f"{name}.png"))

        go(q("Я в ЭКСПО, к 19:00 надо быть в Хан Шатыре", "17:20"), "main")
        pg.click("#how"); pg.wait_for_timeout(900); pg.screenshot(path=str(IMG / "how.png")); pg.click("#closeHow")
        go(q("Я в ЭКСПО, к 19:00 надо быть в Хан Шатыре", "17:20", "cold"), "cold")
        go(q("Хан Шатырдың жанындамын, бір сағат бар, кофе ішкім келеді", "16:00"), "kk")
        go(q("Я у Байтерека, есть 3 часа, хочу провести время интересно", "13:30"), "event",
           after=lambda: (pg.evaluate("document.getElementById('deck').scrollTo({top:5000})"), pg.wait_for_timeout(800)))
        pg.goto(URL); pg.wait_for_timeout(2500)
        pg.evaluate("map.jumpTo({center:[71.4305,51.1283],zoom:16.4,pitch:55,bearing:-25})"); pg.wait_for_timeout(3000)
        xy = pg.evaluate("(()=>{const q=map.project([71.4305,51.1283]);return [q.x,q.y]})()")
        pg.mouse.click(xy[0] + 4, xy[1] + 4); pg.wait_for_selector(".pc [data-a=save]"); pg.wait_for_timeout(5000)
        pg.screenshot(path=str(IMG / "pin.png"))
        ctx.close()
        dev = {k: v for k, v in p.devices["iPhone 15 Pro"].items() if k != "default_browser_type"}
        ph = b.new_context(**dev, geolocation={"latitude": 51.0905, "longitude": 71.418}, permissions=["geolocation"]).new_page()
        ph.goto(q("Я в ЭКСПО, к 19:00 надо быть в Хан Шатыре", "17:20")); ph.wait_for_selector(".sum .l1", timeout=90000); ph.wait_for_timeout(6000)
        ph.screenshot(path=str(IMG / "phone.png"))
        b.close()


def bench():
    t = (ROOT / "docs" / "benchmark.md").read_text(encoding="utf-8")
    row = lambda name: re.search(r"\| " + re.escape(name) + r"[^|]*\| ([^|]+) \| ([^|]+) \|", t)
    out = {}
    for k, name in [("bad", "**Ответов хотя бы с одной ошибкой**"), ("closed", "Визит в **закрытое** место"),
                    ("rush", "Переходы, на которые **физически не хватает времени**"), ("late", "**Опоздания** к сроку")]:
        m = row(name)
        out[k] = (m.group(1).replace("*", "").strip(), m.group(2).replace("*", "").strip()) if m else ("?", "?")
    m = re.search(r"а (\d+)% его остановок невозможно проверить.*?проверяемы (\d+)%", t)
    out["unver"] = (m.group(1), m.group(2)) if m else ("?", "?")
    return out


CSS = """
@page{size:1920px 1080px;margin:0}
*{box-sizing:border-box}
body{margin:0;background:#070a0f;font-family:Manrope,system-ui,sans-serif;color:#e8eef5}
.s{width:1920px;height:1080px;padding:84px 110px;position:relative;overflow:hidden;page-break-after:always;
  background:radial-gradient(1100px 700px at 85% 0%,#12283c 0,#070a0f 60%)}
.s h1{font-size:76px;margin:0 0 18px;letter-spacing:-.02em;line-height:1.08}
.s h2{font-size:56px;margin:0 0 34px;letter-spacing:-.015em;line-height:1.1}
.s p,.s li{font-size:30px;line-height:1.45;color:#c4cfdb}
.s ul{margin:0;padding-left:34px}.s li{margin:8px 0}
.acc{color:#4cc9f0}.ok{color:#3ddc97}.warn{color:#ffb86b}
.n{position:absolute;right:56px;bottom:40px;color:#5d6b7c;font-size:22px}
.tag{display:inline-block;font-size:22px;padding:7px 16px;border-radius:999px;background:rgba(76,201,240,.12);color:#4cc9f0;margin-bottom:26px;font-weight:700}
.grid{display:grid;gap:28px}
.card{background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);border-radius:26px;padding:30px 32px}
.card b{display:block;font-size:54px;color:#4cc9f0;letter-spacing:-.02em;margin-bottom:6px}
.card span{font-size:25px;color:#aab6c4;line-height:1.35;display:block}
.shot{border-radius:22px;border:1px solid rgba(255,255,255,.12);box-shadow:0 30px 80px rgba(0,0,0,.55);width:100%;display:block}
.cap{font-size:22px;color:#8d9aab;margin-top:12px}
.shot.sm{height:318px;object-fit:cover;object-position:left top}
table{border-collapse:collapse;width:100%;font-size:27px}
td,th{padding:16px 18px;border-bottom:1px solid rgba(255,255,255,.08);text-align:left}
th{color:#8d9aab;font-weight:600;font-size:23px}
td.y{color:#3ddc97;font-weight:800}td.no{color:#5d6b7c}
.flow{display:flex;gap:18px;align-items:stretch;margin:10px 0 28px}
.flow .card{flex:1}
.flow .ar{align-self:center;font-size:44px;color:#4cc9f0}
.flow .card h3{margin:0 0 10px;font-size:32px}
.quote{font-size:34px;color:#e8eef5;border-left:5px solid #4cc9f0;padding:6px 0 6px 28px;margin-top:30px;line-height:1.4}
.logo{width:92px;height:92px;border-radius:26px;background:linear-gradient(135deg,#4cc9f0,#8b6cff);display:grid;place-items:center;font-size:52px;font-weight:800;color:#08111a;margin-bottom:34px}
"""


def html(b):
    S = []
    add = S.append
    n = lambda i: f'<div class="n">{i}</div>'
    # 1. титул
    add(f"""<section class="s" style="display:flex;flex-direction:column;justify-content:center">
<div class="logo">О</div><h1 style="font-size:120px">Окно</h1>
<p style="font-size:44px;color:#e8eef5;margin:0 0 40px">Свободное время в Астане — <span class="acc">по пути и вовремя</span></p>
<p>AI for Smart City · кейс НИЦ Smart City «ИИ-помощник по прогулкам в Астане» (RAG)<br>Садуов Рамазан · AITU</p>{n(1)}</section>""")
    # 2. проблема
    add(f"""<section class="s"><span class="tag">Проблема</span><h2>«Что я успею за свободный час — и не опоздаю?»</h2>
<div class="grid" style="grid-template-columns:repeat(3,1fr);margin-bottom:40px">
<div class="card"><b>1,6 млн</b><span>гостей Астаны в 2025, каждый четвёртый — иностранец; ~190 международных событий<br><i style="color:#5d6b7c">акимат, Kazinform, Kazpravda</i></span></div>
<div class="card"><b>65 мин</b><span>пешком от Mega Silk Way до Байтерека: «рядом на карте» здесь — далеко<br><i style="color:#5d6b7c">наш замер, OSRM</i></span></div>
<div class="card"><b>7 мес.</b><span>ветра в году; в октябре +3° ощущается как −1°, закат в 17:20<br><i style="color:#5d6b7c">WeatherSpark, Open-Meteo</i></span></div></div>
<p>ИИ-гиды уверенно ошибаются: у <b class="warn">27%</b> пользователей — неверные часы работы, <b class="warn">16%</b> отправили в несуществующее место
<span style="color:#5d6b7c">(опрос Greetwell, США, 2026 — коммерческий)</span>.</p>
<div class="quote">Карты отвечают «что рядом». Человеку с часом между делами нужно другое: что успеть <b>по пути</b>, чтобы место было <b>открыто, когда дойду</b>, и я не замёрз и не опоздал.</div>{n(2)}</section>""")
    # 3. решение
    add(f"""<section class="s"><span class="tag">Решение</span><h2>Одна фраза → 1–3 места по пути → точно успеешь</h2>
<div class="grid" style="grid-template-columns:1.55fr 1fr;align-items:start">
<div><img class="shot" src="img/main.png"><div class="cap">«Я в ЭКСПО, к 19:00 надо быть в Хан Шатыре» → на месте в 18:43, запас 17 минут</div></div>
<ul style="font-size:30px">
<li><b class="acc">Успеешь точно</b> — реальное время пешком по улицам, пробки 2GIS для такси</li>
<li><b class="acc">Открыто, когда дойдёшь</b> — часы проверяются на весь визит, а не «сейчас»</li>
<li><b class="acc">Знает погоду</b> — прогноз на часы маршрута, мороз, туман, гололёд</li>
<li><b class="acc">Не выдумывает</b> — только реальные места; нет данных — так и пишет</li>
<li><b class="acc">Сам знает, где ты</b> — адресом, с округлением ~150 м</li></ul></div>{n(3)}</section>""")
    # 4. RAG
    add(f"""<section class="s"><span class="tag">Как работает RAG</span><h2>ИИ понимает и объясняет. Места выбирает алгоритм</h2>
<div class="flow">
<div class="card"><h3>1. Понимание</h3><span>Gemini 3.5 Flash: фраза (RU/KZ/EN или голосом) → откуда, куда, к скольки, с кем, событие</span></div><div class="ar">→</div>
<div class="card"><h3>2. Retrieval</h3><span>База <b style="display:inline;font-size:inherit;color:#4cc9f0">1465 мест</b>: OSM + 2GIS + Wikidata. Перебор сотен маршрутов: время, часы, погода, ограничения</span></div><div class="ar">→</div>
<div class="card"><h3>3. Генерация</h3><span>Gemini получает <b style="display:inline;font-size:inherit;color:#4cc9f0">только факты</b> и пишет «почему» и «что делать»</span></div><div class="ar">→</div>
<div class="card"><h3>4. Прозрачность</h3><span>«Как посчитано»: источник у каждого места, надёжность часов, сколько отсеяно и почему</span></div></div>
<div class="grid" style="grid-template-columns:1fr 1fr;align-items:start"><img class="shot" src="img/how.png">
<ul style="font-size:26px"><li>Сбор данных <b>автоматизирован</b>: GitHub Actions — места (OSM, Wikidata) каждую неделю, афиша — каждый день</li>
<li>2GIS Places + Routing: свежие часы, рейтинг, «можно с ноутбуком», пробки</li>
<li>Open-Meteo: погода, прогноз по часам, воздух · OSRM: пешие маршруты</li>
<li>Афиша: официальный сайт Astana Opera</li><li>Без ИИ и сети — работает на правилах и шаблонах</li></ul></div>{n(4)}</section>""")
    # 5. демо — креативность
    add(f"""<section class="s"><span class="tag">Демо: сценарии и фишки</span><h2>Мороз, три языка, афиша, карта как в 2GIS</h2>
<div class="grid" style="grid-template-columns:1fr 1fr;gap:18px 26px;margin-top:-10px">
<div><img class="shot sm" src="img/cold.png"><div class="cap">−25° (демо): маршрут перестраивается — тёплые места, на улице ≤ 12 мин подряд</div></div>
<div><img class="shot sm" src="img/kk.png"><div class="cap">Запрос на казахском — интерфейс и ответ на казахском</div></div>
<div><img class="shot sm" src="img/event.png" style="object-position:left bottom"><div class="cap">Афиша: концерт в Опере помещается в окно — предложит вместо прогулки</div></div>
<div><img class="shot sm" src="img/pin.png"><div class="cap">Нажал на здание — что здесь, фото, адрес, контакты; закладки подсвечивают дом</div></div></div>{n(5)}</section>""")
    # 6. качество
    bad, closed, rush, late, unv = b["bad"], b["closed"], b["rush"], b["late"], b["unver"]
    add(f"""<section class="s"><span class="tag">Качество — цифрами</span><h2>Проверено, а не обещано</h2>
<div class="grid" style="grid-template-columns:repeat(4,1fr);margin-bottom:38px">
<div class="card"><b>44 / 44</b><span>автопроверок в 13 тестовых сценариях (все 5 из кейса + пример из ТЗ)</span></div>
<div class="card"><b class="ok">0</b><span>опозданий и закрытых мест на 100 случайных запросах (стресс-тест)</span></div>
<div class="card"><b>43</b><span>автотеста в GitHub Actions при каждом изменении (ядро + API)</span></div>
<div class="card"><b>~2–6 с</b><span>ответ; без ИИ — запасной режим, демо не зависает</span></div></div>
<p style="margin:0 0 14px"><b style="color:#e8eef5">Те же 30 запросов — «Окну» и обычному ИИ-чатботу</b> (Claude без наших данных), проверка одним скриптом:</p>
<table><tr><th></th><th>Обычный ИИ-чатбот</th><th>Окно</th></tr>
<tr><td>Ответов с ошибкой (закрыто, не успеть, опоздание)</td><td class="warn">{bad[0]}</td><td class="y">{bad[1]}</td></tr>
<tr><td>Визит в закрытое место · физически не успеть · опоздание</td><td class="warn">{closed[0]} · {rush[0]} · {late[0]}</td><td class="y">{closed[1]} · {rush[1]} · {late[1]}</td></tr>
<tr><td>Советов, которые невозможно проверить («прогулка по бульвару»)</td><td class="warn">{unv[0]}%</td><td class="y">{100 - int(unv[1]) if unv[1].isdigit() else "?"}%</td></tr></table>{n(6)}</section>""")
    # 7. отличия
    Y, N = '<td class="y">да</td>', '<td class="no">—</td>'
    add(f"""<section class="s"><span class="tag">Чем мы отличаемся</span><h2>Мы не конкурируем с картами — мы решаем «что успеть»</h2>
<table><tr><th></th><th>Google Maps (Ask Maps)</th><th>2GIS ИИ</th><th>ChatGPT / ИИ-гиды</th><th>Окно</th></tr>
<tr><td>План под окно времени А → Б</td>{N}{N}<td>частично</td>{Y}</tr>
<tr><td>Часы работы на момент прихода</td>{N}{N}<td class="warn">ошибается</td>{Y}</tr>
<tr><td>Погода, ветер, прогноз в маршруте</td>{N}{N}{N}{Y}</tr>
<tr><td>Пробки для такси</td>{Y}{Y}{N}<td class="y">да (2GIS)</td></tr>
<tr><td>Казахский · русский · английский</td><td>только EN</td><td>RU</td>{Y}{Y}</tr>
<tr><td>Афиша, помещается ли событие в окно</td>{N}{N}{N}{Y}</tr>
<tr><td>Честно: «данных нет», источник у каждого места</td>{N}{N}{N}{Y}</tr></table>
<p style="margin-top:26px;font-size:24px;color:#8d9aab">Ask Maps — 150+ стран с 08.2026, только на английском; ИИ-помощник 2GIS отвечает про одно место, а не строит план.</p>{n(7)}</section>""")
    # 8. этика
    add(f"""<section class="s"><span class="tag">Этика и приватность</span><h2>Минимум данных, максимум честности</h2>
<div class="grid" style="grid-template-columns:1fr 1fr">
<ul><li>Геопозиция округляется до <b class="acc">~150 м</b>, не хранится и не пишется в логи</li>
<li>Нет аккаунтов и истории; закладки — <b class="acc">только в браузере</b></li>
<li>Голос распознаёт сам браузер; записи у нас нет</li>
<li>Закон РК «Об ИИ» (с 18.01.2026): сгенерированное помечено</li></ul>
<ul><li>«Открыто» — только по данным; иначе <b class="warn">«оценка»</b></li>
<li>Место выбирает алгоритм — ИИ не может придумать кафе</li>
<li>Мороз — ограничение времени на улице; ночью — без парков</li>
<li>Не успеть — <b class="acc">честный отказ</b> и совет, а не выдуманный маршрут</li></ul></div>
<div class="quote">Пример: «хочу в музей» в 7 утра → «Музей Первого Президента: сегодня выходной» — и то, что открыто сейчас.</div>{n(8)}</section>""")
    # 9. бизнес
    add(f"""<section class="s"><span class="tag">Бизнес и польза городу</span><h2>Для кого и как внедряется</h2>
<div class="grid" style="grid-template-columns:repeat(3,1fr)">
<div class="card"><b style="font-size:40px">B2G</b><span>Акимат, Visit Astana, Smart Astana: модуль «свободное время» в городских сервисах; обезличенная статистика спроса — где не хватает тёплых мест и кафе</span></div>
<div class="card"><b style="font-size:40px">B2B</b><span>Аэропорт NQZ, отели, EXPO, арены, Astana Opera: виджет «у вас 2 часа до вылета / до матча / до спектакля»</span></div>
<div class="card"><b style="font-size:40px">Партнёры</b><span>Места и афиши — с пометкой «партнёр», без влияния на правило «успеешь и открыто»</span></div></div>
<p style="margin-top:36px">Польза городу: гости распределяются по <b class="acc">малому бизнесу по пути</b>, а не только по трём главным ТРЦ; данные о спросе — для городского планирования.</p>
<p>Уже в прототипе: страница <b class="acc">«Спрос для города»</b> — обезличенно, что ищут люди и <b class="warn">чего им не хватило</b> (каток закрыт в нужное время, нет часов работы в данных, смотровые далеко).</p>
<p>Себестоимость: ~0,1 цента за запрос (Gemini Flash), данные бесплатные или по ключу 2GIS.</p>{n(9)}</section>""")
    # 10. риски и план
    add(f"""<section class="s"><span class="tag">Ограничения, риски, план</span><h2>Честно о слабых местах — и что дальше</h2>
<div class="grid" style="grid-template-columns:1fr 1fr">
<div><p style="color:#e8eef5;font-weight:700">Ограничения и риски</p><ul style="font-size:30px">
<li>OSM неполный (часы — у 36%) → 2GIS и пометка «оценка»</li>
<li>Демо-ключ 2GIS: 50 маршрутов/сутки → кэш, своя оценка; для запуска — платный ключ</li>
<li>Нет открытого GTFS автобусов/LRT → не обещаем</li>
<li>Афиша — пока один источник (Astana Opera)</li></ul></div>
<div><p style="color:#e8eef5;font-weight:700">Что дальше</p><ul style="font-size:30px">
<li>Пилот с аэропортом NQZ или EXPO</li><li>Общественный транспорт и LRT при открытом GTFS</li>
<li>Афиша: Ticketon, Sxodim, арены — через партнёрство</li><li>Доступность: пандусы и лифты с городскими сообществами</li>
<li>Telegram-мини-приложение</li></ul></div></div>{n(10)}</section>""")
    # 11. финал
    add(f"""<section class="s" style="display:grid;grid-template-columns:1.1fr .9fr;gap:60px;align-items:center">
<div><div class="logo">О</div><h1 style="font-size:100px">Окно</h1><p style="font-size:40px;color:#e8eef5">Свободное время — по пути и вовремя.</p>
<p style="margin-top:40px">GitHub: <span class="acc">github.com/rsduv/okno-astana</span><br>Документ со сценариями: docs/Okno_scenarios.pdf<br>Демо-видео: YouTube (ссылка в форме сдачи)</p>
<div class="quote">У каждого гостя Астаны есть окно. Мы делаем так, чтобы оно не пропадало.</div></div>
<div style="display:flex;justify-content:center"><img class="shot" src="img/phone.png" style="width:auto;height:880px"></div>{n(11)}</section>""")
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Окно — презентация</title>
<link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;600;700;800&display=swap" rel="stylesheet"><style>{CSS}</style></head>
<body>{''.join(S)}</body></html>"""


def main():
    if "--no-shots" not in sys.argv:
        shots()
    DIR.mkdir(parents=True, exist_ok=True)
    page = DIR / "okno_slides.html"
    page.write_text(html(bench()), encoding="utf-8")
    pdf = ROOT / "docs" / "Okno_presentation.pdf"
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True)
        pg = b.new_page(viewport={"width": 1920, "height": 1080})
        pg.goto(page.as_uri()); pg.wait_for_timeout(2500)
        pg.pdf(path=str(pdf), width="1920px", height="1080px", print_background=True, margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})
        for i in (1, 6):
            pg.evaluate(f"window.scrollTo(0, {(i - 1) * 1080})"); pg.wait_for_timeout(300)
            pg.screenshot(path=str(DIR / f"preview_{i}.png"))
        b.close()
    print(page, pdf)


if __name__ == "__main__":
    main()
