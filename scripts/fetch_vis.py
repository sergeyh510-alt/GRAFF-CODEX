#!/usr/bin/env python
"""Download a local copy of vis-network for fully offline use.

The frontend loads vis-network **local-first**: it tries
``static/vendor/vis-network.min.js`` and only falls back to a CDN when the
local copy is missing or fails.  Run this script once (it needs internet) to
vendor the library into the project:

    python scripts/fetch_vis.py

Afterwards GRAFF works with no network access at all.
"""
from __future__ import annotations

import os
import sys
import urllib.request

URLS = [
    "https://unpkg.com/vis-network/standalone/umd/vis-network.min.js",
    "https://cdn.jsdelivr.net/npm/vis-network/standalone/umd/vis-network.min.js",
]

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.path.join(PROJECT_ROOT, "static", "vendor", "vis-network.min.js")


def main() -> int:
    os.makedirs(os.path.dirname(TARGET), exist_ok=True)
    last_error = None
    for url in URLS:
        try:
            print("[vis] downloading %s" % url)
            with urllib.request.urlopen(url, timeout=30) as response:
                data = response.read()
            if not data:
                raise RuntimeError("empty response")
            with open(TARGET, "wb") as handle:
                handle.write(data)
            print("[vis] saved %d bytes -> %s" % (len(data), TARGET))
            return 0
        except Exception as exc:
            last_error = exc
            print("[vis] failed: %s" % exc)
    print("[vis] could not download vis-network: %s" % last_error)
    print("[vis] the CDN copy will still be used when the browser has internet.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
