#!/usr/bin/env python
"""Draw the layout idea for the 3D view: current sphere versus a river of time.

The figure is a concept drawing, not a screenshot: a synthetic session (turns
with parallel tool calls, results, a few errors) is laid out twice, once the way
a force simulation folds a long chain into a ball today, and once as a stream
where the axes carry meaning.

Usage:
    python scripts/draw_layout_idea.py docs/figures/layout-idea.png
"""

from __future__ import annotations

import sys

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

BG = "#0b1018"
FG = "#c9d4e3"
GRID = "#1b2534"

KIND = {
    "reason": ("#94a3b8", 26, "рассуждение"),
    "call": ("#3b82f6", 34, "вызов инструмента"),
    "result": ("#2dd4bf", 22, "результат"),
    "error": ("#ef4444", 46, "ошибка"),
    "answer": ("#e2e8f0", 30, "ответ"),
}


def build_session(seed: int = 11):
    """Return nodes as (turn, order, branch, kind, parent) and an index of turns."""
    rng = np.random.default_rng(seed)
    nodes: list[tuple[int, int, float, str, int | None]] = []
    turn_rows: list[tuple[int, int]] = []
    for turn in range(16):
        start = len(nodes)
        calls = int(rng.integers(3, 9))
        nodes.append((turn, 0, 0.0, "reason", None))
        for i in range(calls):
            spread = i - (calls - 1) / 2
            nodes.append((turn, 1 + 2 * i, spread, "call", start))
            nodes.append((turn, 2 + 2 * i, spread, "result", len(nodes) - 1))
        nodes.append((turn, 1 + 2 * calls, 0.0, "answer", start))
        if turn % 4 == 2:
            nodes.append((turn, 2 + 2 * calls, calls / 3, "error", start))
        turn_rows.append((start, len(nodes) - 1))
    return nodes, turn_rows


def sphere_layout(count: int):
    """Golden spiral on a sphere: what a curled chain looks like when forces win."""
    index = np.arange(count) + 0.5
    phi = np.arccos(1 - 2 * index / count)
    theta = np.pi * (1 + 5 ** 0.5) * index
    radius = 1.0 + 0.03 * np.sin(index * 2.1)
    x = radius * np.sin(phi) * np.cos(theta)
    y = radius * np.sin(phi) * np.sin(theta)
    z = radius * np.cos(phi)
    return x, y, z


def river_layout(nodes, turn_rows):
    """Time along one axis, parallelism across, turn depth in the third."""
    total = len(nodes)
    span = 9.0
    x = np.zeros(total)
    y = np.zeros(total)
    z = np.zeros(total)
    for i, (turn, order, branch, kind, _parent) in enumerate(nodes):
        x[i] = span * (i / max(total - 1, 1))
        y[i] = branch * 0.34
        z[i] = -turn * 0.30 + 0.06 * np.sin(i * 0.7)
        if kind == "answer":
            y[i] = 0.0
    return x, y, z


def style_axes(ax) -> None:
    ax.set_facecolor(BG)
    for axis in (ax.xaxis, ax.yaxis, ax.zaxis):
        axis.set_pane_color((0.043, 0.063, 0.098, 1.0))
        axis.line.set_color(GRID)
    ax.tick_params(colors=GRID, labelcolor=GRID, labelsize=7)
    ax.grid(False)


def draw(ax, nodes, x, y, z, title: str, subtitle: str) -> None:
    for i, (_turn, _order, _branch, _kind, parent) in enumerate(nodes):
        if parent is None:
            continue
        ax.plot(
            [x[parent], x[i]],
            [y[parent], y[i]],
            [z[parent], z[i]],
            color="#38506f",
            linewidth=0.45,
            alpha=0.45,
            zorder=1,
        )
    for kind, (color, size, _label) in KIND.items():
        picked = [i for i, node in enumerate(nodes) if node[3] == kind]
        if not picked:
            continue
        ax.scatter(
            x[picked],
            y[picked],
            z[picked],
            s=size * 3.2,
            c=color,
            alpha=0.10,
            edgecolors="none",
            depthshade=False,
        )
        ax.scatter(
            x[picked],
            y[picked],
            z[picked],
            s=size,
            c=color,
            alpha=0.95,
            edgecolors="none",
            depthshade=False,
        )
    ax.set_title(title, color=FG, fontsize=13, pad=38, loc="left")
    ax.text2D(0.01, 1.055, subtitle, transform=ax.transAxes, ha="left", color="#8b9bb4", fontsize=10)
    ax.set_axis_off()
    ax.view_init(elev=22, azim=-58)


def main(argv: list[str]) -> int:
    target = argv[1] if len(argv) > 1 else "docs/figures/layout-idea.png"
    nodes, turn_rows = build_session()

    figure = plt.figure(figsize=(15.5, 7.4), dpi=140, facecolor=BG)
    left = figure.add_subplot(1, 2, 1, projection="3d")
    right = figure.add_subplot(1, 2, 2, projection="3d")

    style_axes(left)
    style_axes(right)

    sx, sy, sz = sphere_layout(len(nodes))
    draw(
        left,
        nodes,
        sx,
        sy,
        sz,
        "Сейчас: физика стягивает цепочку в шар",
        "цепочка свёрнута спиралью, рёбра уходят внутрь, направление не видно",
    )

    rx, ry, rz = river_layout(nodes, turn_rows)
    draw(
        right,
        nodes,
        rx,
        ry,
        rz,
        "Идея: русло времени, рукава и слои",
        "время вдоль оси, параллельные вызовы в рукава, ходы слоями в глубину",
    )

    right.text(0.2, -0.4, 0.0, "исток", color="#a9b8cc", fontsize=11)
    right.text(8.3, 0.95, -4.9, "устье", color="#a9b8cc", fontsize=11)

    handles = [
        plt.Line2D([], [], marker="o", linestyle="", markersize=7, markerfacecolor=color, markeredgecolor="none", label=label)
        for _kind, (color, _size, label) in KIND.items()
    ]
    legend = figure.legend(
        handles=handles,
        loc="lower center",
        ncol=5,
        frameon=False,
        fontsize=9,
        bbox_to_anchor=(0.5, 0.01),
    )
    for text in legend.get_texts():
        text.set_color(FG)

    figure.suptitle(
        "Почему при тысячах узлов получается клубок, и что меняет русло",
        color=FG,
        fontsize=14.5,
        y=0.985,
    )
    figure.subplots_adjust(left=0.01, right=0.99, top=0.80, bottom=0.09)
    figure.savefig(target, facecolor=BG)
    print("saved", target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
