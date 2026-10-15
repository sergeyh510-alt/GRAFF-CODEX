#!/usr/bin/env python
"""Render the Mermaid blocks of an article into PNG files.

Usage:
    python scripts/make_diagrams.py docs/linkedin-article-en.md

The script pairs each markdown image reference of the form

    ![alt](figures/NAME.png)

with the first ```mermaid fence that follows it, renders the diagram through
mermaid.ink and stores the result as docs/figures/NAME.png next to the article.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
import urllib.request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) GRAFF-docs/1.0"
ENDPOINT = "https://mermaid.ink/img/{payload}?type=png"

IMAGE_RE = re.compile(r"!\[[^\]]*\]\((?P<path>[^)]+)\)")
FENCE_RE = re.compile(r"```mermaid\s*\n(?P<code>.*?)```", re.DOTALL)


def pairs(text: str) -> list[tuple[str, str]]:
    """Return (image path, mermaid source) pairs in document order."""
    found: list[tuple[str, str]] = []
    fences = list(FENCE_RE.finditer(text))
    for image in IMAGE_RE.finditer(text):
        fence = next((f for f in fences if f.start() > image.end()), None)
        if fence is None:
            continue
        found.append((image.group("path"), fence.group("code").strip()))
        fences.remove(fence)
    return found


def render(code: str) -> bytes:
    body = json.dumps({"code": code, "mermaid": {"theme": "default"}}).encode("utf-8")
    url = ENDPOINT.format(payload=base64.urlsafe_b64encode(body).decode("ascii"))
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                return response.read()
        except Exception as error:  # noqa: BLE001 - retry network hiccups
            last_error = error
            time.sleep(3 * (attempt + 1))
    raise last_error if last_error else RuntimeError("no response")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    article = os.path.abspath(argv[1])
    base = os.path.dirname(article)
    with open(article, encoding="utf-8") as handle:
        text = handle.read()

    items = pairs(text)
    if not items:
        print("no mermaid blocks found")
        return 1

    for index, (rel_path, code) in enumerate(items, start=1):
        target = os.path.join(base, rel_path.replace("/", os.sep))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        try:
            data = render(code)
        except Exception as error:  # noqa: BLE001 - keep the loop going
            print(f"[{index}/{len(items)}] {rel_path} FAILED: {error}")
            continue
        with open(target, "wb") as handle:
            handle.write(data)
        print(f"[{index}/{len(items)}] {rel_path} {len(data)} bytes")
        time.sleep(0.6)

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
