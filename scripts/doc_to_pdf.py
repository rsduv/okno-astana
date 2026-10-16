"""docs/scenarios.md -> docs/Okno_scenarios.pdf (красивый PDF для жюри). python scripts/doc_to_pdf.py"""
from pathlib import Path
import markdown
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
md = (ROOT / "docs" / "scenarios.md").read_text(encoding="utf-8")
body = markdown.markdown(md, extensions=["tables"])
html = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
@page {{ size: A4; margin: 16mm 14mm; }}
body {{ font-family: Manrope, sans-serif; color:#16202b; font-size:10.5pt; line-height:1.5; }}
h1 {{ font-size:22pt; margin:0 0 6px; letter-spacing:-.02em; }}
h1 + p {{ color:#5b6878; font-size:9.5pt; }}
h2 {{ font-size:15pt; margin:22px 0 8px; padding-top:8px; border-top:2px solid #4cc9f0; }}
h3 {{ font-size:12pt; margin:18px 0 6px; break-after:avoid; }}
table {{ border-collapse:collapse; width:100%; margin:8px 0 10px; font-size:9pt; break-inside:auto; }}
th, td {{ border:1px solid #d9e0e8; padding:5px 6px; vertical-align:top; text-align:left; }}
th {{ background:#eef4f9; }}
tr {{ break-inside:avoid; }}
code {{ background:#eef2f6; padding:1px 4px; border-radius:4px; font-size:9pt; }}
img {{ width:100%; border-radius:8px; border:1px solid #d9e0e8; margin:6px 0 2px; break-inside:avoid; }}
em {{ color:#5b6878; }}
ul {{ padding-left:18px; }}
strong {{ color:#0b1520; }}
.cover {{ display:flex; align-items:center; gap:12px; margin-bottom:6px; }}
.logo {{ width:40px; height:40px; border-radius:11px; background:linear-gradient(135deg,#4cc9f0,#8b6cff); color:#08111a;
        font-weight:800; font-size:20px; display:flex; align-items:center; justify-content:center; }}
</style></head><body>
<div class="cover"><div class="logo">О</div><div><b>Окно</b> · Build with AI Hackathon, трек AI for Smart City</div></div>
{body}</body></html>"""
tmp = ROOT / "docs" / "_scenarios_print.html"
tmp.write_text(html, encoding="utf-8")
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True)
    pg = b.new_page()
    pg.goto(tmp.as_uri())
    pg.wait_for_timeout(1500)
    pg.pdf(path=str(ROOT / "docs" / "Okno_scenarios.pdf"), format="A4", print_background=True,
           margin={"top": "16mm", "bottom": "16mm", "left": "14mm", "right": "14mm"})
    b.close()
tmp.unlink() if "--keep" not in __import__("sys").argv else None
print("docs/Okno_scenarios.pdf")
