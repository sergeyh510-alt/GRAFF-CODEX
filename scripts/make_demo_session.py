#!/usr/bin/env python
r"""Generate a synthetic Codex rollout so GRAFF can be demoed without a real run.

This is a thin CLI wrapper around :mod:`graff.demo`; the server exposes the same
functionality via the optional ``--demo`` flag.

Usage:
    python scripts/make_demo_session.py
    python scripts/make_demo_session.py --turns 8 --interval 0.5 --out demo.jsonl
    python scripts/make_demo_session.py --interval 0.7 --keep-open

    # a LARGE session for performance measurements (~3900 nodes, ~1:1 links):
    python scripts/make_demo_session.py --nodes 3900 --out runtime\demo\big.jsonl
    python run.py --session runtime\demo\big.jsonl --port 8770
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from graff.demo import generate_demo_session

#: How many graph nodes one demo turn produces (measured).
EVENTS_PER_TURN = 6.0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a synthetic Codex rollout for GRAFF demos.")
    parser.add_argument("--turns", type=int, default=6, help="number of activity turns (default: 6)")
    parser.add_argument("--nodes", type=int, default=None,
                        help="target graph-node count; overrides --turns "
                             "(each turn yields ~6 nodes).  Use 3900 for a stress session.")
    parser.add_argument("--interval", type=float, default=0.0,
                        help="seconds to wait between turns (use with --keep-open for a live demo)")
    parser.add_argument("--keep-open", action="store_true",
                        help="keep appending new turns until interrupted (live demo)")
    parser.add_argument("--out", default=None, help="output path (default: runtime/demo/rollout-demo.jsonl)")
    parser.add_argument("--model", default="deepseek/deepseek-v3.1", help="model name written into session_meta")
    parser.add_argument("--seed", type=int, default=None, help="random seed for reproducible output")
    args = parser.parse_args(argv)

    turns = args.turns
    if args.nodes:
        turns = max(1, int(round(args.nodes / EVENTS_PER_TURN)))
        print("[demo] target ~%d nodes -> %d turns (%.1f nodes/turn)" % (args.nodes, turns, EVENTS_PER_TURN))

    out = args.out or os.path.join(PROJECT_ROOT, "runtime", "demo", "rollout-demo.jsonl")
    path = generate_demo_session(out, turns=turns, interval=args.interval,
                                 keep_open=args.keep_open, model=args.model, seed=args.seed)
    print("[demo] wrote %s" % path)
    print("[demo] run it with: python run.py --session %s" % path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
