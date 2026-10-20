"""Прогон интерфейса как пользователь: клики, ошибки консоли, скриншоты в shots/ux_*.png"""
from playwright.sync_api import sync_playwright
OUT = "shots/ux_"
errs = []
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True)
    for name, vp, scale in (("desk", {"width": 1440, "height": 900}, 1), ("phone", {"width": 390, "height": 844}, 2)):
        pg = b.new_page(viewport=vp, device_scale_factor=scale)
        pg.on("console", lambda m: errs.append(f"{name}: {m.text}") if m.type == "error" else None)
        pg.on("pageerror", lambda e: errs.append(f"{name} PAGEERROR: {e}"))
        pg.goto("http://127.0.0.1:8010/"); pg.wait_for_timeout(3500); pg.screenshot(path=OUT + name + "_1home.png")
        assert pg.is_visible("#intro.show"), "нет карточки первого визита"
        pg.click("#introOk"); pg.wait_for_timeout(300)
        pg.click("#exBtn"); pg.wait_for_timeout(300); pg.click("#quick button[data-i='0']"); pg.wait_for_selector(".sum .l1, .sum .err", timeout=90000); pg.wait_for_timeout(4000)
        pg.screenshot(path=OUT + name + "_2result.png")
        pg.click("#deck .card[data-i='1'] .swap"); pg.wait_for_selector(".sum .l1, .sum .err", timeout=90000); pg.wait_for_timeout(4000)
        pg.screenshot(path=OUT + name + "_3swap.png")
        pg.click("#how"); pg.wait_for_timeout(800); pg.screenshot(path=OUT + name + "_4how.png")
        pg.click("#closeHow"); pg.fill("#q", "jdjdjd"); pg.click("#go"); pg.wait_for_selector(".sum .err", timeout=150000); pg.wait_for_timeout(500)
        pg.screenshot(path=OUT + name + "_5err.png")
        if name == "desk":   # карточка «что здесь» → закладка: метка, кнопка и подсветка здания — сразу, без перезагрузки
            pg.evaluate("map.jumpTo({center:[71.4305,51.1283],zoom:16.4,pitch:50})"); pg.wait_for_timeout(2500)
            xy = pg.evaluate("(()=>{const q=map.project([71.4305,51.1283]);return [q.x,q.y]})()")
            pg.mouse.click(xy[0] + 4, xy[1] + 4); pg.wait_for_selector(".pc [data-a=save]", timeout=20000)
            pg.click(".pc [data-a=save]"); pg.click(".savepop .kinds button[data-k='work']"); pg.click("#svOk"); pg.wait_for_timeout(1200)
            got = pg.evaluate("[document.querySelectorAll('.mk.bm').length, document.querySelectorAll('.pill.sv').length]")
            if got != [1, 1]:
                errs.append(f"закладка не появилась сразу: {got}")
            pg.screenshot(path=OUT + name + "_6bookmark.png")
        pg.close()
    b.close()
print("\n".join(errs) or "no console errors")
