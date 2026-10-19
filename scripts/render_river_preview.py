#!/usr/bin/env python
"""Render the river scene dump to a PNG, so the brightness is verifiable offline.

This is a concept render, not a browser screenshot: it reads .work/river_scene.json
(node coordinates and types, plus the edge list) and draws the same picture the
new river mode draws -- a saturated core per node, a separate additive halo pass,
and camera-facing ribbon links coloured as a gradient from cause to effect.

Usage:
    python scripts/render_river_preview.py .work/river_scene.json docs/screenshots/app-river-bright.png
"""

from __future__ import annotations

import json
import sys

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.collections import LineCollection  # noqa: E402

BG = "#04070c"
PALETTE = {
    "reason": "#a855f7",
    "message": "#e2f3ff",
    "tool_call": "#2b8cff",
    "tool_result": "#00e5a0",
    "file_edit": "#ffb020",
    "error": "#ff2d55",
    "user": "#d8b4fe",
    "start": "#7dff5a",
    "end": "#ffd60a",
    "cold": "#3b2a7d",
}
TYPE_KEYS = ["reason", "message", "tool_call", "tool_result", "file_edit", "error", "user"]
TYPE_GAIN = [1.28, 0.76, 1.38, 1.32, 1.32, 1.8, 1.02]
TYPE_COLD = [0.94, 0.94, 0.94, 0.94, 0.9, 0.5, 0.94]
COLD_MIN = 0.10
COLD_DECAY = 1.7
BUCKETS = 128


def rgb(hex_color: str) -> np.ndarray:
    h = hex_color.lstrip("#")
    return np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], dtype=float)


def colour_table():
    """The same table static/js/river.js builds, so the render matches it."""
    table = {}
    for slot, key in enumerate(TYPE_KEYS):
        base = rgb(PALETTE[key])
        cold = rgb(PALETTE["cold"])
        gain = TYPE_GAIN[slot]
        keep = TYPE_COLD[slot]
        if key == "error":
            cold = np.array([0x6A, 0x10, 0x33], dtype=float)
        rows = []
        for b in range(BUCKETS):
            f = b / (BUCKETS - 1)
            k = (COLD_MIN + (1 - COLD_MIN) * f ** COLD_DECAY) * keep
            rows.append(np.clip((base + (cold - base) * k) * gain, 0, 255))
        table[key] = np.array(rows)
    return table


def main() -> None:
    src = sys.argv[1] if len(sys.argv) > 1 else ".work/river_scene.json"
    dst = sys.argv[2] if len(sys.argv) > 2 else "docs/screenshots/app-river-bright.png"
    with open(src, encoding="utf-8") as handle:
        scene = json.load(handle)

    pts = scene["pts"]
    table = colour_table()
    x = np.array([p["x"] for p in pts], dtype=float)
    z = np.array([p["z"] for p in pts], dtype=float)
    # time normalised over the whole stream: 0 = oldest, 1 = newest
    frac = np.linspace(0.0, 1.0, len(pts)) if len(pts) > 1 else np.zeros(1)
    frac = 1.0 - frac                      # index 0 is the oldest event
    bucket = np.clip((frac * (BUCKETS - 1)).astype(int), 0, BUCKETS - 1)
    colours = np.zeros((len(pts), 3))
    for i, p in enumerate(pts):
        key = p["type"] if p["type"] in table else "message"
        colours[i] = table[key][bucket[i]]

    fig, ax = plt.subplots(figsize=(14, 6.2), dpi=110)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)

    index_of = {p_id: i for i, p_id in enumerate(n["id"] for n in scene["pts"])}
    # edges as gradients from the parent colour to the child colour
    segs, seg_cols = [], []
    for a, b in scene["edges"]:
        ia, ib = index_of.get(a), index_of.get(b)
        if ia is None or ib is None:
            continue
        segs.append([(x[ia], z[ia]), (x[ib], z[ib])])
        # the ribbon reads as a gradient from cause to effect
        seg_cols.append((colours[ia] + colours[ib]) / 2.0 / 255.0)
    if segs:
        rgba = np.concatenate([np.asarray(seg_cols), np.full((len(seg_cols), 1), 0.5)], axis=1)
        ax.add_collection(LineCollection(segs, colors=rgba, linewidths=1.6, zorder=1))

    # the additive halo pass: the same nodes, bigger and very transparent
    ax.scatter(x, z, s=340, c=colours / 255.0, alpha=0.30, linewidths=0,
               edgecolors="none", zorder=2)
    ax.scatter(x, z, s=150, c=colours / 255.0, alpha=0.55, linewidths=0,
               edgecolors="none", zorder=3)
    # the saturated core
    ax.scatter(x, z, s=42, c=colours / 255.0, linewidths=0, edgecolors="none", zorder=4)

    ax.set_xlim(x.min() - 40, x.max() + 40)
    ax.set_ylim(z.min() - 40, z.max() + 40)
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.set_title("River: saturated cores + additive halos + ribbon links",
                 color="#c9d4e3", fontsize=11, pad=10)

    handles = [plt.Line2D([], [], marker="o", linestyle="", markersize=6,
                          markerfacecolor=PALETTE[k], markeredgecolor="none", label=k)
               for k in ("reason", "message", "tool_call", "tool_result", "file_edit", "error")]
    legend = ax.legend(handles=handles, loc="lower right", ncol=3, frameon=False,
                       fontsize=8, labelcolor="#c9d4e3")
    legend.get_frame().set_alpha(0.0)

    fig.tight_layout()
    fig.savefig(dst, facecolor=BG)
    print("wrote", dst, "nodes", len(pts), "edges", len(segs))


if __name__ == "__main__":
    main()
