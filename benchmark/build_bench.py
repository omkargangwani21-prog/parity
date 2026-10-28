#!/usr/bin/env python3
"""Build the Parity benchmark from extracted CodeNet COBOL programs.

Selection rule (fixed, documented, no hand-picking):
  1. Keep only submissions whose CodeNet status is "Accepted".
  2. Keep a submission only if it compiles with GnuCOBOL (cobc) and its stdout on the
     problem's sample input matches the sample output (whitespace-trimmed). This is the
     "reproducible" filter.
  3. At most ONE program per problem: the first valid one in submission-id order.
  4. Drop programs longer than --max-lines non-blank lines (default 100).
  5. Randomly sample --count programs using a fixed --seed (default 42).

Usage:  python build_bench.py <bench_raw_dir> <out_bench_dir> [--count 40] [--seed 42]
Output: <out>/cn_<pid>_<sid>.cob, cn_<pid>_<sid>.1.in, and <out>/manifest.json
"""
import argparse
import csv
import json
import os
import random
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

COBC = os.environ.get("COBC", "cobc")


def run(cmd, stdin=None, timeout=20):
    try:
        p = subprocess.run(cmd, input=stdin, capture_output=True, text=True,
                           timeout=timeout, errors="replace")
        return p.returncode, p.stdout
    except subprocess.TimeoutExpired:
        return 124, ""


def norm(s):
    lines = [ln.rstrip() for ln in s.replace("\r\n", "\n").split("\n")]
    return "\n".join(lines).strip()


def accepted_ids(meta_csv):
    ids = set()
    if not meta_csv.exists():
        return ids
    with open(meta_csv, newline="", errors="replace") as f:
        for row in csv.DictReader(f):
            sid = row.get("submission_id") or row.get("id")
            if sid and row.get("status") == "Accepted":
                ids.add(sid)
    return ids


def check(src, sample_in, sample_out):
    """Return (ok, fmt). Tries free format first, then fixed."""
    with tempfile.TemporaryDirectory() as td:
        exe = Path(td) / "prog"
        for fmt in ("-free", "-fixed"):
            rc, _ = run([COBC, "-x", fmt, "-o", str(exe), str(src)], timeout=60)
            if rc != 0 or not exe.exists():
                continue
            rc, out = run([str(exe)], stdin=sample_in)
            if rc == 0 and norm(out) == norm(sample_out):
                return True, fmt
            return False, fmt
    return False, None


def nonblank_lines(path):
    return sum(1 for ln in path.read_text(errors="replace").splitlines() if ln.strip())


def process_problem(pdir):
    pid = pdir.name
    io = pdir / "io"
    if not (io / "input.txt").exists() or not (io / "output.txt").exists():
        return None
    sample_in = (io / "input.txt").read_text(errors="replace")
    sample_out = (io / "output.txt").read_text(errors="replace")
    acc = accepted_ids(pdir / "meta" / f"{pid}.csv")
    for cob in sorted((pdir / "cobol").glob("s*.*")):
        if cob.stem not in acc:
            continue
        ok, fmt = check(cob, sample_in, sample_out)
        if ok:
            return {"pid": pid, "sid": cob.stem, "path": str(cob), "fmt": fmt,
                    "lines": nonblank_lines(cob)}
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("raw")
    ap.add_argument("out")
    ap.add_argument("--count", type=int, default=40)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--max-lines", type=int, default=100)
    ap.add_argument("--jobs", type=int, default=4)
    a = ap.parse_args()

    pdirs = sorted(p for p in Path(a.raw).iterdir() if p.is_dir())
    print(f"{len(pdirs)} problems", file=sys.stderr)
    valid = []
    with ThreadPoolExecutor(a.jobs) as ex:
        for i, r in enumerate(ex.map(process_problem, pdirs), 1):
            if r:
                valid.append(r)
            if i % 25 == 0:
                print(f"  checked {i}/{len(pdirs)} problems, {len(valid)} valid so far",
                      file=sys.stderr)

    short = [v for v in valid if v["lines"] <= a.max_lines]
    pool = sorted(short, key=lambda v: (v["pid"], v["sid"]))
    picked = random.Random(a.seed).sample(pool, min(a.count, len(pool)))
    picked.sort(key=lambda v: (v["pid"], v["sid"]))

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for v in picked:
        name = f"cn_{v['pid']}_{v['sid']}"
        src = Path(v["path"])
        (out / f"{name}.cob").write_text(src.read_text(errors="replace"))
        sample_in = (src.parent.parent / "io" / "input.txt").read_text(errors="replace")
        (out / f"{name}.1.in").write_text(sample_in)
        v["name"] = name
        del v["path"]
    (out / "manifest.json").write_text(json.dumps(
        {"rule": __doc__, "seed": a.seed, "max_lines": a.max_lines,
         "problems_total": len(pdirs), "valid_reproducible": len(valid),
         "after_length_filter": len(short), "selected": picked}, indent=2))

    fmts = {}
    for v in picked:
        fmts[v["fmt"]] = fmts.get(v["fmt"], 0) + 1
    print(f"\nproblems: {len(pdirs)} | valid & reproducible: {len(valid)} | "
          f"<= {a.max_lines} lines: {len(short)} | selected: {len(picked)}")
    print("formats among selected:", fmts)
    print("written to", out)


if __name__ == "__main__":
    main()
