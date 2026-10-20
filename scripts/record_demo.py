"""Автоматическая запись демо-видео «Окна» (для YouTube): реальный браузер, реальные ответы, подписи на экране.

Запуск (сервер на :8010): python scripts/record_demo.py  →  video/okno_demo.mp4 (папка video/ — в .gitignore)
"""
import shutil
import subprocess
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "video"
URL = "http://127.0.0.1:8010/"
W, H = 1920, 1080

CAP_CSS = """
#cap{position:fixed;left:calc(50% + 290px);bottom:42px;transform:translateX(-50%);z-index:99;max-width:1180px;width:max-content;padding:16px 28px;border-radius:18px;
  background:rgba(8,12,18,.88);border:1px solid rgba(255,255,255,.14);color:#fff;font:600 30px/1.35 Manrope,system-ui,sans-serif;
  text-align:center;box-shadow:0 18px 60px rgba(0,0,0,.6);transition:opacity .35s;opacity:0;pointer-events:none}
#cap.on{opacity:1}
#cap b{color:#4cc9f0}
#title{position:fixed;inset:0;z-index:100;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:18px;
  background:radial-gradient(1200px 700px at 50% 40%,#16324a,#070a0f);color:#fff;font-family:Manrope,system-ui,sans-serif;transition:opacity .6s}
#title h1{margin:0;font-size:96px;letter-spacing:-.02em}
#title p{margin:0;font-size:36px;color:#b9c6d6;text-align:center;line-height:1.4}
#title .k{display:flex;gap:26px;margin-top:22px}
#title .k div{padding:18px 26px;border-radius:18px;background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.12);font-size:26px;text-align:center}
#title .k b{display:block;font-size:46px;color:#4cc9f0}
"""


def main():
    OUT.mkdir(exist_ok=True)
    tmp = OUT / "raw"
    shutil.rmtree(tmp, ignore_errors=True)
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True)
        ctx = b.new_context(viewport={"width": W, "height": H}, record_video_dir=str(tmp), record_video_size={"width": W, "height": H},
                            geolocation={"latitude": 51.0905, "longitude": 71.4180}, permissions=["geolocation"], locale="ru-RU")
        pg = ctx.new_page()
        pg.add_init_script("try{localStorage.clear();localStorage.setItem('okno_intro','1')}catch(e){}")
        pg.goto(URL)
        pg.add_style_tag(content=CAP_CSS)
        pg.evaluate("""() => { const c = document.createElement('div'); c.id = 'cap'; document.body.appendChild(c); }""")
        w = pg.wait_for_timeout
        t_start = time.time()
        chapters = []
        mark = lambda title: chapters.append((time.time() - t_start, title))   # главы для YouTube — по реальному времени записи

        def title(h1, sub, kpis=None, ms=3800):
            k = "".join(f"<div><b>{a}</b>{b}</div>" for a, b in (kpis or []))
            pg.evaluate("""([h, s, k]) => { let t = document.getElementById('title'); if(!t){ t = document.createElement('div'); t.id='title'; document.body.appendChild(t); }
                t.style.opacity = 1; t.innerHTML = `<h1>${h}</h1><p>${s}</p>` + (k ? `<div class="k">${k}</div>` : ''); }""", [h1, sub, k])
            w(ms)
            pg.evaluate("() => { const t = document.getElementById('title'); t.style.opacity = 0; setTimeout(() => t.remove(), 700); }")
            w(700)

        def cap(text, ms=0):
            pg.evaluate("t => { const c = document.getElementById('cap'); c.innerHTML = t; c.classList.add('on'); }", text)
            if ms:
                w(ms)

        def uncap():
            pg.evaluate("() => document.getElementById('cap').classList.remove('on')")

        def ask(text, t=None, delay=38):
            pg.evaluate("t => { setTime(t); }", t)
            pg.fill("#q", "")
            pg.click("#q")
            pg.type("#q", text, delay=delay)
            w(400)
            pg.click("#go")
            pg.wait_for_selector(".sum .l1, .sum .err", timeout=90000)
            w(2600)

        def scroll_deck(y, ms=1800):
            pg.evaluate("y => document.getElementById('deck').scrollTo({top: y, left: y, behavior: 'smooth'})", y)
            w(ms)

        # 0. заставка
        mark("Окно — что это")
        w(2500)
        title("Окно", "Свободное время в Астане — по пути и вовремя.<br>RAG-помощник: реальные места, часы работы на момент прихода, погода и пробки.")
        # 0.5 карточка первого визита
        mark("Первое открытие: что это и как пользоваться")
        pg.evaluate("() => showIntro()")
        w(900)
        cap("При первом открытии — короткая карточка: <b>что это и как пользоваться</b>, и сразу про приватность", 5200)
        uncap()
        pg.click("#introOk")
        w(700)
        # 1. где я
        mark("Само знает, где ты и какая погода")
        cap("«Окно» само определяет, <b>где ты</b> — адресом, с округлением до ~150 м — и какая <b>погода</b> сейчас", 4200)
        cap("Новичку подсказка прямо в строке поиска: можно писать <b>как другу</b>", 3800)
        uncap()
        # 2. главный сценарий
        mark("«Я в ЭКСПО, к 19:00 надо в Хан Шатыр» — маршрут с запасом")
        cap("Задача: я в ЭКСПО, в 19:00 встреча в Хан Шатыре. <b>Что успею по пути?</b>")
        ask("Я в ЭКСПО, к 19:00 надо быть в Хан Шатыре, хочу по пути что-нибудь посмотреть", "17:20")
        # цифры в подписи — из реального ответа (маршрут зависит от пробок и погоды в момент записи)
        arr, buf = pg.evaluate("() => [state.last.summary.arrive, state.last.summary.buffer_min]")
        cap(f"Маршрут: <b>на месте в {arr}, запас {buf} мин</b>. Полоса — пешком, такси, остановки, запас", 4500)
        cap("У каждого места — <b>почему</b>, <b>что делать</b>, часы <b>на момент прихода</b> и источник (OSM · 2GIS · Википедия)", 4500)
        scroll_deck(420, 2600)
        scroll_deck(0, 1200)
        # 3. как посчитано
        mark("«Как посчитано?» — прозрачность RAG, почему не другие места")
        cap("«Как посчитано?» — честно показываем, что сделал ИИ, а что алгоритм")
        pg.click("#how")
        w(2200)
        cap("<b>1465 мест → кандидаты → сотни маршрутов</b>. Места выбирает алгоритм, ИИ только пишет текст по фактам", 4200)
        cap("Датасет собран <b>автоматически</b> (OSM, Wikidata, 2GIS) и обновляется сам каждую неделю — открытая таблица в репозитории", 4200)
        pg.evaluate("() => document.getElementById('sheetBox').scrollTo({top: 420, behavior: 'smooth'})")
        cap("«Почему не другие места»: сколько отсеяно — <b>закрыто к приходу, не успеть, мороз</b>", 3800)
        pg.click("#closeHow")
        w(500)
        # 4. мороз
        mark("Мороз −25° (демо): маршрут через тёплые места")
        cap("Октябрь, +2°. А если <b>−25° и ветер</b>? Нажимаем на плашку погоды (демо зимы)")
        w(1600)
        pg.click("#wnow")
        pg.wait_for_timeout(1200)
        pg.wait_for_selector(".sum .l1, .sum .err", timeout=90000)
        w(3000)
        cap("Маршрут <b>перестроился</b>: тёплые места и короткие перебежки на такси, на улице не больше 12 минут подряд", 5200)
        pg.click("#wnow")
        w(2500)
        uncap()
        # 5. казахский
        mark("Запрос на казахском")
        cap("Запрос <b>на казахском</b> — интерфейс и ответ сами переключаются на казахский")
        ask("Хан Шатырдың жанындамын, бір сағат бар, кофе ішкім келеді", "16:00", delay=34)
        w(1500)
        cap("Кофейня рядом, вернуться к Хан Шатыру через час — <b>RU · KZ · EN</b>", 3800)
        pg.click("#langs button[data-l='ru']")
        w(800)
        # 5.5 «Где я?» по фото
        mark("«Где я?» по фото — Gemini Vision")
        pg.evaluate("() => { clearRoute(); document.getElementById('deck').innerHTML=''; document.getElementById('sum').className='sum glass'; state.last=null; }")
        cap("Турист не знает, как называется место? <b>Сфотографируй</b> — кнопка с камерой")
        w(1800)
        pg.set_input_files("#camIn", str(ROOT / "tests" / "photo_bayterek.jpg"))
        pg.wait_for_function("() => !document.getElementById('cam').classList.contains('busy')", timeout=60000)
        w(1200)
        cap("<b>Gemini Vision</b> узнал Байтерек — старт маршрута там. Фото не сохраняется", 3600)
        ask("Есть час, хочу кофе и вернуться", "15:00")
        cap("Маршрут — от места на фото", 3000)
        uncap()
        # 5.7 встретиться посередине
        mark("Встретиться с другом посередине")
        pg.evaluate("() => clearPoint()")
        cap("Группы: <b>встретиться с другом посередине</b>")
        ask("Я у Хан Шатыра, друг в ЭКСПО, где встретиться выпить кофе?", "15:00")
        mt = pg.evaluate("() => state.last && state.last.meet")
        if mt:
            cap(f"«{mt['name']}»: тебе ~{mt['you_min']} мин, другу ~{mt['friend_min']} мин — встреча в {mt['time']}. <b>Путь друга</b> — фиолетовым", 5200)
        uncap()
        # 6. афиша города
        mark("Афиша города: выставки, квизы, концерты")
        pg.evaluate("() => clearPoint()")   # старт с фото больше не нужен — дальше места из текста запроса
        cap("<b>Афиша города</b>: Astana Opera + sxodim.com — выставки, квизы, концерты. Только по теме и если помещается в окно")
        ask("Я у Байтерека, есть 2 часа, хочу на выставку", "14:00")
        scroll_deck(5000, 2400)
        ev = pg.evaluate("() => (state.last.events || [])[0] || null")
        if ev and not ev.get("later"):
            cap(f"«{ev['title'][:60]}» — дойдёшь к {ev['time']}, ~{ev['go_min']} мин {'пешком' if ev['mode'] == 'walk' else 'на такси'}, и успеешь вернуться. Цена и билеты", 5200)
        else:
            cap("Если событие не помещается в окно — его не предлагаем, показываем ближайшие даты", 4800)
        scroll_deck(0, 900)
        uncap()
        # 6.5 LRT
        mark("LRT Астаны на карте: 18 станций из OpenStreetMap")
        pg.evaluate("() => { clearRoute(); document.getElementById('deck').innerHTML=''; document.getElementById('sum').className='sum glass'; state.last=null; }")
        pg.evaluate("() => { map.flyTo({center:[71.432,51.118], zoom:12.6, pitch:0, bearing:0, duration:2400}); }")
        w(2800)
        cap("Линия <b>LRT</b> (открыта 16 мая 2026) и все 18 станций — из OpenStreetMap", 4200)
        cap("На длинных отрезках «Окно» подсказывает: <b>можно на LRT</b> от станции X до Y — время честно помечено как оценка", 4600)
        uncap()
        # 7. карта как в 2GIS
        mark("Карта как в 2GIS: карточка здания, закладки")
        cap("Карта как в 2GIS: нажми на здание — <b>что здесь</b>, фото, адрес, контакты")
        pg.evaluate("() => { clearRoute(); document.getElementById('deck').innerHTML=''; document.getElementById('sum').className='sum glass'; state.last=null; }")
        pg.evaluate("() => { map.flyTo({center:[71.4305,51.1283], zoom:16.4, pitch:55, bearing:-25, duration:2600}); }")
        w(3200)
        xy = pg.evaluate("() => { const q = map.project([71.4305,51.1283]); return [q.x, q.y]; }")
        pg.mouse.click(xy[0] + 4, xy[1] + 4)
        pg.wait_for_selector(".pc [data-a=save]", timeout=20000)
        w(4500)
        cap("«В закладки» — выбираешь значок, <b>здание подсвечивается</b> цветом закладки", 1500)
        pg.click(".pc [data-a=save]")
        w(900)
        pg.click(".savepop .kinds button[data-k='work']")
        w(900)
        pg.click("#svOk")
        w(1200)
        w(3600)   # без облёта камеры: в 3D на этом зуме запись без видеокарты зависает
        uncap()
        # 8. честный отказ
        mark("Честный отказ, если не успеть")
        cap("А если <b>не успеть</b>? «Окно» не выдумывает маршрут")
        ask("Я в ЭКСПО, через 30 минут надо быть на вокзале Астана-1", "12:00")
        said = pg.inner_text(".sum")
        if "даже на такси" in said:
            cap("Честно: <b>не успеть даже на такси</b> — лучше предупредить, что опоздаешь. Без выдуманных маршрутов", 5200)
        else:
            cap("Честно: пешком не успеть — <b>бери такси</b>. Без выдуманных маршрутов", 5200)
        uncap()
        # 8.5 спрос для города
        mark("Спрос для города (B2G)")
        pg.goto(URL + "city")
        pg.add_style_tag(content=CAP_CSS)
        pg.evaluate("""() => { const c = document.createElement('div'); c.id = 'cap'; c.style.left = '50%'; document.body.appendChild(c); }""")
        w(1800)
        cap("Для города: <b>обезличенный спрос</b> — что ищут люди и <b>чего им не хватило</b>. Без координат и текстов запросов", 5200)
        pg.evaluate("() => window.scrollTo({top: 600, behavior: 'smooth'})")
        w(2600)
        uncap()
        # 9. итог
        mark("Итоги проверок")
        title("Окно", "Проверено, а не обещано",
              [("115 / 120", "ответов без выдумок<br>(судья сверяет с данными)"), ("120 / 120", "синтетических<br>запросов"),
               ("0 из 30", "ответов с ошибкой<br>(тот же Gemini без данных — 4)"), ("RU · KZ · EN", "три языка")],
              ms=6500)
        w(500)
        ctx.close()
        b.close()
    txt = "\n".join(f"{int(t // 60):02d}:{int(t % 60):02d} {title}" for t, title in [(0, chapters[0][1])] + chapters[1:])
    (OUT / "chapters.txt").write_text(txt + "\n", encoding="utf-8")
    print(txt)
    raw = next(tmp.glob("*.webm"))
    mp4 = OUT / "okno_demo.mp4"
    ff = shutil.which("ffmpeg") or "ffmpeg"
    subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(raw), "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(mp4)], check=True)
    shutil.rmtree(tmp, ignore_errors=True)
    print(mp4, round(mp4.stat().st_size / 1e6, 1), "MB")


if __name__ == "__main__":
    t0 = time.time()
    main()
    print(round(time.time() - t0), "s")
