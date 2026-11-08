# -*- coding: utf-8 -*-
"""Compare two text files without loading either into the model's context.

    python textdiff.py A B [--srt] [--max-lines 80]

Prints: encoding/BOM, size, line and (for --srt) cue counts, Simplified vs Traditional character counts,
a similarity ratio, and a unified diff capped at --max-lines. Read THIS output; open excerpts with read_file
only where the diff points. Written 2026-10-07 after five subtitle comparisons each cost 100-500k tokens
because both whole files sat in context every turn.
"""
import argparse
import difflib
import re
import sys
from pathlib import Path

TRAD = set("這個們來時說為後會國學對開關門體與長經車馬鳥魚實現點應樣機產業動務總統關係")
SIMP = set("这个们来时说为后会国学对开关门体与长经车马鸟鱼实现点应样机产业动务总统关系")


def load(path):
    raw = Path(path).read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    for enc in ("utf-8-sig", "utf-8", "gb18030", "big5", "utf-16"):
        try:
            return raw.decode(enc), enc, bom
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8?", bom


def describe(name, text, enc, bom, srt):
    lines = text.splitlines()
    out = [f"{name}: {len(text):,} chars, {len(lines):,} lines, encoding {enc}{' +BOM' if bom else ''}"]
    if srt:
        cues = re.findall(r"^\d+\s*$", text, re.M)
        times = re.findall(r"(\d\d:\d\d:\d\d,\d{3}) --> (\d\d:\d\d:\d\d,\d{3})", text)
        out.append(f"   cues {len(cues)}, timestamp lines {len(times)}, first {times[0][0] if times else '-'}, last {times[-1][1] if times else '-'}")
    t = sum(c in TRAD for c in text); s = sum(c in SIMP for c in text)
    out.append(f"   traditional-marker chars {t}, simplified-marker chars {s} -> {'traditional' if t > s else 'simplified' if s > t else 'undetermined'}")
    return "\n".join(out)


def main():
    if sys.stdout is not None:   # Windows consoles default to cp1252; subtitle text is CJK
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("a"); ap.add_argument("b")
    ap.add_argument("--srt", action="store_true", help="count subtitle cues and timestamps")
    ap.add_argument("--max-lines", type=int, default=80, help="cap on diff lines printed")
    args = ap.parse_args()
    ta, ea, ba = load(args.a); tb, eb, bb = load(args.b)
    print(describe("A", ta, ea, ba, args.srt)); print(describe("B", tb, eb, bb, args.srt))
    la, lb = ta.splitlines(), tb.splitlines()
    ratio = difflib.SequenceMatcher(None, la, lb, autojunk=False).ratio() if max(len(la), len(lb)) < 20000 else difflib.SequenceMatcher(None, ta[:200000], tb[:200000]).ratio()
    print(f"similarity (line-level) {ratio:.3f}")
    diff = list(difflib.unified_diff(la, lb, fromfile="A", tofile="B", lineterm="", n=1))
    changed = sum(1 for d in diff if d[:1] in "+-" and d[:3] not in ("+++", "---"))
    print(f"diff: {changed} changed lines; showing first {min(args.max_lines, len(diff))} of {len(diff)} diff lines")
    for d in diff[:args.max_lines]:
        print("  " + d)
    if len(diff) > args.max_lines:
        print(f"  ... {len(diff) - args.max_lines} more; rerun with --max-lines or read_file at the line numbers above")
    return 0


if __name__ == "__main__":
    sys.exit(main())
