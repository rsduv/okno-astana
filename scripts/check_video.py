"""Проверка демо-видео: не мелькает ли подпись на экране слишком коротко (её не успеют прочитать).

Берём 5 кадров в секунду, смотрим на область подписи внизу экрана: если подпись поменялась и продержалась меньше
MIN секунд — печатаем время. python scripts/check_video.py [video/okno_demo_short.mp4]
"""
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
FPS, MIN = 5, 2.5
W, H = 960, 540


def frames(path):
    ff = shutil.which("ffmpeg") or "ffmpeg"
    p = subprocess.Popen([ff, "-loglevel", "error", "-i", str(path), "-vf", f"fps={FPS},scale={W}:{H},format=gray",
                          "-f", "rawvideo", "-"], stdout=subprocess.PIPE)
    while True:
        buf = p.stdout.read(W * H)
        if len(buf) < W * H:
            break
        yield buf


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "video" / "okno_demo_short.mp4"
    # подпись — плашка внизу по центру-справа (x ≈ 330…920, y ≈ 455…525 на кадре 960×540)
    segs, cur, start, i = [], None, 0, 0
    for i, f in enumerate(frames(path)):
        grid = [f[y * W + x] > 200 for y in range(455, 525, 2) for x in range(330, 920, 3)]   # без numpy: редкая сетка
        text = sum(grid) / len(grid)                     # доля ярких пикселей — есть ли подпись
        sig = grid if text > 0.01 else None
        diff = (sum(a != b for a, b in zip(sig, cur)) / len(sig)) if sig is not None and cur is not None else 0
        changed = (sig is None) != (cur is None) or diff > 0.04
        if changed:
            if cur is not None:
                segs.append((start / FPS, i / FPS))
            cur, start = sig, i
    if cur is not None:
        segs.append((start / FPS, i / FPS))
    short = [(a, b) for a, b in segs if b - a < MIN]
    print(f"подписей: {len(segs)}, коротких (< {MIN} с): {len(short)}")
    for a, b in short:
        print(f"  {int(a // 60):02d}:{a % 60:05.2f} — {b - a:.1f} с")


if __name__ == "__main__":
    main()
