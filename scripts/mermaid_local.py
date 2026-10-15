#!/usr/bin/env python
"""Render Mermaid sources to PNG locally, without any online service.

Usage:
    python scripts/mermaid_local.py docs/diagrams/*.mmd

For every .mmd file next to it the script writes a small HTML page that pulls
the bundled mermaid.min.js, opens it in headless Chrome (fallback: Edge),
takes a screenshot and trims the white margins with Pillow. Result: a .png with
the same base name in docs/figures/.
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile

from PIL import Image

BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<style>
  html, body {{ margin: 0; padding: 0; background: #ffffff; }}
  body {{ padding: 20px; font-family: "Segoe UI", Arial, sans-serif; }}
</style>
<script src="{mermaid}"></script>
</head><body>
<pre class="mermaid">{code}</pre>
<script>mermaid.initialize({{ startOnLoad: true, theme: "default" }});</script>
</body></html>
"""

PAD = 18
WINDOW = "2000,1500"
MERMAID_DIR = os.path.join(os.getcwd(), "docs", "diagrams")

IMAGE_RE = re.compile(r"!\[[^\]]*\]\((?P<path>[^)]+)\)")
FENCE_RE = re.compile(r"```mermaid\s*\n(?P<code>.*?)```", re.DOTALL)


def pairs_from_markdown(path: str) -> list[tuple[str, str]]:
    """Pair every image reference in a markdown file with the next mermaid fence."""
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    found: list[tuple[str, str]] = []
    fences = list(FENCE_RE.finditer(text))
    for image in IMAGE_RE.finditer(text):
        fence = next((item for item in fences if item.start() > image.end()), None)
        if fence is None:
            continue
        found.append((image.group("path").replace("/", os.sep), fence.group("code").strip()))
        fences.remove(fence)
    return found


def find_browser() -> str:
    for candidate in BROWSERS:
        if os.path.exists(candidate):
            return candidate
    raise SystemExit("no Chrome or Edge found")


def trim(path: str) -> tuple[int, int]:
    image = Image.open(path).convert("RGB")
    mask = image.convert("L").point(lambda value: 0 if value > 245 else 255)
    box = mask.getbbox()
    if box:
        left = max(box[0] - PAD, 0)
        top = max(box[1] - PAD, 0)
        right = min(box[2] + PAD, image.width)
        bottom = min(box[3] + PAD, image.height)
        image = image.crop((left, top, right, bottom))
    image.save(path, "PNG")
    return image.size


def render(browser: str, base: str, code: str, out_path: str) -> None:
    mermaid = os.path.join(MERMAID_DIR, "mermaid.min.js")
    if not os.path.exists(mermaid):
        raise SystemExit(f"mermaid.min.js not found in {MERMAID_DIR}")
    os.makedirs(MERMAID_DIR, exist_ok=True)
    html_path = os.path.join(MERMAID_DIR, base + ".render.html")
    with open(html_path, "w", encoding="utf-8") as handle:
        handle.write(PAGE.format(mermaid="mermaid.min.js", code=code))

    target = out_path
    os.makedirs(os.path.dirname(target), exist_ok=True)
    profile = tempfile.mkdtemp(prefix="mmd-")
    command = [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--no-first-run",
        "--no-default-browser-check",
        f"--user-data-dir={profile}",
        f"--window-size={WINDOW}",
        "--virtual-time-budget=15000",
        f"--screenshot={target}",
        "file:///" + html_path.replace("\\", "/"),
    ]
    subprocess.run(command, check=True, capture_output=True, timeout=180)
    shutil.rmtree(profile, ignore_errors=True)
    os.remove(html_path)
    width, height = trim(target)
    print(f"{os.path.basename(target)}: {width}x{height}")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2

    browser = find_browser()
    figures_dir = os.path.join(os.getcwd(), "docs", "figures")
    os.makedirs(figures_dir, exist_ok=True)

    jobs: list[tuple[str, str, str]] = []
    for argument in argv[1:]:
        if argument.lower().endswith(".md"):
            article_dir = os.path.dirname(os.path.abspath(argument))
            for rel, code in pairs_from_markdown(argument):
                base = os.path.splitext(os.path.basename(rel))[0]
                jobs.append((base, code, os.path.join(article_dir, rel)))
            continue
        for mmd in glob.glob(argument):
            base = os.path.splitext(os.path.basename(mmd))[0]
            with open(mmd, encoding="utf-8") as handle:
                code = handle.read().strip()
            jobs.append((base, code, os.path.join(figures_dir, base + ".png")))

    if not jobs:
        print("nothing to render")
        return 1

    for base, code, out_path in jobs:
        render(browser, base, code, out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
