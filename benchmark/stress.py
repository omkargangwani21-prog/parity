#!/usr/bin/env python3
"""Parity stress test: re-check already-verified Java on extra, model-generated inputs.

For each solved case in a benchmark run:
  1. Ask the large model for N extra stdin inputs (it sees the COBOL source and the sample input).
  2. Run the real COBOL program (GnuCOBOL) on each. Inputs it rejects (non-zero exit) are dropped.
  3. Run the Java that was already verified on the sample input, and compare outputs.
No new translation happens, so this costs one short model call per program.

Usage:  python stress.py results/<run-folder> bench [--extra 4] [--jobs 3] [--limit N]
Writes: results/<run-folder>/stress.json
"""
import argparse
import json
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import parity

GEN_SYSTEM = ("You write test inputs for COBOL programs. Reply with only a JSON array of strings and "
              "nothing else. Each string is one complete standard input for the program, in exactly "
              "the format the program reads.")


def gen_inputs(cobol, sample, n):
    msg = (f"COBOL program:\n{cobol}\n\nA sample input (one JSON string):\n{json.dumps(sample)}\n\n"
           f"Write {n} different valid inputs in the same format. Cover different cases: small "
           "values, large values near the limits the PIC clauses allow, zeros or equal values, and "
           f"typical values. Do not repeat the sample. Reply with a JSON array of {n} strings only.")
    r = parity.chat(parity.model_for("ultra"),
                    [{"role": "system", "content": GEN_SYSTEM}, {"role": "user", "content": msg}], 3000)
    m = re.search(r"\[.*\]", r["text"], re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return []
    out = []
    for s in arr:
        if isinstance(s, str) and s.strip() and s != sample and len(s) <= 2000:
            s = s if s.endswith("\n") else s + "\n"
            if s not in out:
                out.append(s)
    return out[:n]


def stress_one(name, java, bench, n):
    res = {"name": name, "valid": 0, "match": 0, "mismatches": [], "note": None}
    cob = bench / f"{name}.cob"
    if not cob.exists():
        res["note"] = "COBOL file not found"
        return res
    samp = bench / f"{name}.1.in"
    sample = samp.read_text(errors="replace") if samp.exists() else ""
    try:
        cands = gen_inputs(cob.read_text(errors="replace"), sample, n)
    except Exception as e:  # noqa: BLE001
        res["note"] = f"input generation failed: {type(e).__name__}"
        return res
    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        exe = wd / "cobol_prog"
        _, err = parity.compile_cobol(cob, exe)
        if err:
            res["note"] = err
            return res
        valid = []
        for t in cands:
            rc, out, _ = parity.run([str(exe)], stdin=t, timeout=20)
            if rc == 0:
                valid.append((t, parity.norm(out)))
        if not valid:
            res["note"] = "no usable generated input"
            return res
        (wd / "Main.java").write_text(java)
        rc, _, jerr = parity.run(["javac", "Main.java"], cwd=wd, timeout=60)
        if rc != 0:
            res["note"] = "Java did not compile"
            return res
        for t, exp in valid:
            rc, o, e = parity.run(["java", "-Xmx256m", "-cp", str(wd), "Main"], stdin=t, timeout=20)
            res["valid"] += 1
            if rc == 0 and parity.norm(o) == exp:
                res["match"] += 1
            else:
                res["mismatches"].append({"stdin": t[:300], "expected": exp[:300],
                                          "actual": parity.norm(o)[:300],
                                          "error": e.strip()[:200] if rc else None})
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("bench")
    ap.add_argument("--extra", type=int, default=4)
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    run, bench = Path(a.run), Path(a.bench)
    cases = []
    for f in sorted((run / "cases").glob("*.json")):
        c = json.loads(f.read_text())
        if c.get("solved") and c.get("java"):
            cases.append((c["name"], c["java"]))
    if a.limit:
        cases = cases[:a.limit]
    print(f"{len(cases)} solved programs; asking for {a.extra} extra inputs each", flush=True)
    results = []
    with ThreadPoolExecutor(a.jobs) as ex:
        for r in ex.map(lambda x: stress_one(x[0], x[1], bench, a.extra), cases):
            results.append(r)
            tag = r["note"] or f"{r['match']}/{r['valid']} extra inputs match"
            print(f"  {r['name']:<28} {tag}", flush=True)
    tested = [r for r in results if r["valid"]]
    clean = [r for r in tested if r["match"] == r["valid"]]
    bad = [r for r in tested if r["match"] != r["valid"]]
    print()
    print(f"programs stressed        : {len(tested)} of {len(results)} (others: no usable extra input or error)")
    print(f"extra inputs checked     : {sum(r['valid'] for r in tested)}")
    print(f"extra inputs matching    : {sum(r['match'] for r in tested)}")
    print(f"programs matching on all : {len(clean)} of {len(tested)}")
    for r in bad:
        print(f"  MISMATCH in {r['name']}: {len(r['mismatches'])} input(s)")
    (run / "stress.json").write_text(json.dumps(results, indent=2))
    print(f"details: {run / 'stress.json'}")


if __name__ == "__main__":
    main()
