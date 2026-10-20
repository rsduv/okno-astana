"""Компактная версия демо-видео (~2,5 мин): из полной записи (scripts/record_demo.py) берём только сильные главы.

    python scripts/make_short.py  →  video/okno_demo_short.mp4 + video/chapters_short.txt
"""
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
V = ROOT / "video"
KEEP = ["Окно — что это", "Зачем", "Я в ЭКСПО", "QR", "Как это работает", "Мороз", "по фото", "Встретиться",
        "Афиша города", "Честный отказ", "Что дальше", "Итоги"]
MAX_SEC: dict = {}   # сцены целиком: обрезка начала сцены оставляла подписи на долю секунды


def main():
    ff = shutil.which("ffmpeg") or "ffmpeg"
    src = V / "okno_demo.mp4"
    info = subprocess.run([ff, "-i", str(src)], capture_output=True, text=True).stderr   # длительность — из «Duration: 00:05:22.40»
    h, m, sec = re.search(r"Duration: (\d+):(\d+):([\d.]+)", info).groups()
    dur = int(h) * 3600 + int(m) * 60 + float(sec)
    ch = []
    for line in (V / "chapters.txt").read_text(encoding="utf-8").splitlines():
        m = re.match(r"(\d+):(\d+) (.+)", line.strip())
        if m:
            ch.append((int(m.group(1)) * 60 + int(m.group(2)), m.group(3)))
    segs, out_ch, t_out = [], [], 0.0
    for i, (t0, name) in enumerate(ch):
        t1 = ch[i + 1][0] if i + 1 < len(ch) else dur
        key = next((k for k in KEEP if k in name), None)
        if not key:
            continue
        if key == "Итоги":
            t1 = min(t1, t0 + 7.7)   # видео заканчивается на итоговой заставке
        length = t1 - t0
        cap = MAX_SEC.get(key)
        start = t0
        if cap and length > cap:
            start = t1 - cap       # ожидание ответа — в начале главы, итог и подписи — в конце: берём конец
        segs.append((start, t1))
        out_ch.append((t_out, name))
        t_out += t1 - start
    parts = "".join(f"[0:v]trim={a:.2f}:{b:.2f},setpts=PTS-STARTPTS[v{i}];" for i, (a, b) in enumerate(segs))
    fc = parts + "".join(f"[v{i}]" for i in range(len(segs))) + f"concat=n={len(segs)}:v=1:a=0[out]"
    dst = V / "okno_demo_short.mp4"
    subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(src), "-filter_complex", fc, "-map", "[out]",
                    "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(dst)], check=True)
    txt = "\n".join(f"{int(t // 60):02d}:{int(t % 60):02d} {n}" for t, n in out_ch)
    (V / "chapters_short.txt").write_text(txt + "\n", encoding="utf-8")
    print(txt)
    print(dst, f"{t_out / 60:.1f} мин", round(dst.stat().st_size / 1e6, 1), "MB")


if __name__ == "__main__":
    main()
