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

GEN_SYSTEM = ("You write test inputs for COBOL programs. Each input is the complete standard input "
              "of one run, in exactly the format the program reads. Output only the inputs, each one "
              "starting with a line containing exactly ### INPUT and ending with a line containing "
              "exactly ### END. No JSON, no quotes, no explanations.")


def parse_inputs(text, sample, n):
    """Parse '### INPUT ... ### END' blocks; fall back to a JSON array of strings."""
    found = re.findall(r"###\s*INPUT[ \t]*\r?\n(.*?)\r?\n?###\s*END", text, re.S)
    if not found:
        m = re.search(r"\[.*\]", text, re.S)
        if m:
            try:
                found = [x for x in json.loads(m.group(0), strict=False) if isinstance(x, str)]
            except Exception:  # noqa: BLE001
                found = []
    out = []
    for s in found:
        s = s.strip("\r\n")
        if not s.strip():
            continue
        s += "\n"
        if s != sample and len(s) <= 2000 and s not in out:
            out.append(s)
    return out[:n]


def gen_inputs(cobol, sample, n):
    """Returns (inputs, raw_reply). raw_reply lets the caller say why nothing usable came back."""
    msg = (f"COBOL program:\n{cobol}\n\nA sample input:\n### INPUT\n{sample.rstrip(chr(10))}\n### END\n\n"
           f"Write {n} different valid inputs in the same format. Cover different cases: small "
           "values, large values within the limits the PIC clauses allow, zeros or equal values, and "
           "typical values. Inspect every IF/ELSE and every EVALUATE/WHEN arm in the COBOL and "
           "choose inputs intended to exercise each branch, including both sides of each condition. "
           "Do not repeat the sample. Use the ### INPUT / ### END format for each. After the input "
           "blocks, add a short 'BRANCHES NOT TARGETED:' line naming any branch you could not "
           "target, or write 'BRANCHES NOT TARGETED: none'.")
    r = parity.chat(parity.model_for("ultra"),
                    [{"role": "system", "content": GEN_SYSTEM}, {"role": "user", "content": msg}], 3000)
    return parse_inputs(r["text"], sample, n), r["text"]


def stress_one(name, java, bench, n):
    res = {"name": name, "valid": 0, "match": 0, "generated": 0,
           "rejected": 0, "timed_out": 0, "unusable": 0,
           "requested": n, "branch_coverage": "not measured (generator intent only)",
           "mismatches": [], "note": None}
    cob = bench / f"{name}.cob"
    if not cob.exists():
        res["note"] = "COBOL file not found"
        return res
    samp = bench / f"{name}.1.in"
    sample = samp.read_text(errors="replace") if samp.exists() else ""
    try:
        cands, raw = gen_inputs(cob.read_text(errors="replace"), sample, n)
    except Exception as e:  # noqa: BLE001
        res["unusable"] = n
        res["note"] = f"input generation failed: {type(e).__name__}"
        return res
    if not cands:
        res["unusable"] = n
        res["note"] = "model returned no parseable inputs: " + repr((raw or "")[:200])
        return res
    res["generated"] = len(cands)
    res["unusable"] = max(0, n - len(cands))
    if len(cands) < n:
        res["note"] = f"only {len(cands)} of {n} requested inputs generated"
    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        exe = wd / "cobol_prog"
        _, err = parity.compile_cobol(cob, exe)
        if err:
            res["note"] = err
            return res
        valid = []
        for t in cands:
            rc, out, _ = parity.run([str(exe)], stdin=t, timeout=20, tmpdir=wd)
            if rc == 0:
                valid.append((t, parity.norm(out)))
            elif rc == 124:
                res["timed_out"] += 1
            elif rc == 125:
                res["unusable"] += 1
            else:
                res["rejected"] += 1
        if not valid:
            res["note"] = "no usable generated input"
            return res
        (wd / "Main.java").write_text(java)
        rc, _, jerr = parity.run(["javac", "Main.java"], cwd=wd, timeout=60, tmpdir=wd)
        if rc != 0:
            res["unusable"] += len(valid)
            res["note"] = "Java did not compile"
            return res
        for t, exp in valid:
            rc, o, e = parity.run(["java", "-Xmx256m", "-cp", str(wd), "Main"], stdin=t,
                                  timeout=20, cwd=wd, tmpdir=wd)
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
    cases = {}
    for f in sorted((run / "cases").glob("*.json")):
        c = json.loads(f.read_text())
        if c.get("solved") and c.get("java"):
            cases[c["name"]] = c["java"]
    # Include every benchmark program so unsolved or missing cases are explicitly reported.
    names = sorted(p.stem for p in bench.glob("*.cob"))
    work = []
    for name in names:
        if name in cases:
            work.append((name, cases[name]))
        else:
            work.append((name, None))
    if a.limit:
        work = work[:a.limit]
    print(f"{sum(java is not None for _, java in work)} solved programs among {len(work)} programs; asking for {a.extra} extra inputs each", flush=True)
    results = []
    with ThreadPoolExecutor(a.jobs) as ex:
        for r in ex.map(lambda x: stress_one(x[0], x[1], bench, a.extra) if x[1] else
                        {"name": x[0], "valid": 0, "match": 0, "generated": 0,
                         "rejected": 0, "timed_out": 0, "unusable": a.extra,
                         "requested": a.extra, "branch_coverage": "not measured",
                         "mismatches": [], "note": "no saved solved Java translation"}, work):
            results.append(r)
            partial = bool(r["note"] or r["rejected"] or r["timed_out"] or r["unusable"]
                           or r["valid"] < r["requested"])
            r["partial"] = partial
            detail = (f"{r['match']}/{r['valid']} usable match; rejected={r['rejected']}, "
                      f"timed_out={r['timed_out']}, unusable={r['unusable']}")
            tag = ("PARTIAL - " + (r["note"] + "; " if r["note"] else "") + detail
                   if partial else f"ALL {r['match']}/{r['requested']} inputs match")
            print(f"  {r['name']:<28} {tag}", flush=True)
    tested = [r for r in results if r["valid"]]
    clean = [r for r in results if not r["partial"] and r["match"] == r["requested"]]
    bad = [r for r in tested if r["match"] != r["valid"]]
    partial = [r for r in results if r["partial"]]
    print()
    print(f"programs stressed        : {len(tested)} of {len(results)} (programs with at least one usable extra input)")
    print(f"extra inputs checked     : {sum(r['valid'] for r in tested)}")
    print(f"extra inputs matching    : {sum(r['match'] for r in tested)}")
    print(f"programs fully checked   : {len(clean)} of {len(results)}")
    print(f"PARTIAL programs         : {len(partial)}")
    print("branch coverage          : not measured; generated inputs target branches but execution paths are not instrumented")
    for r in results:
        if r["partial"]:
            print(f"  PARTIAL {r['name']}: {r['note'] or 'some generated candidates were rejected, timed out, or unusable'}; "
                  f"rejected={r['rejected']}, timed_out={r['timed_out']}, unusable={r['unusable']}")
    for r in bad:
        print(f"  MISMATCH in {r['name']}: {len(r['mismatches'])} input(s)")
    (run / "stress.json").write_text(json.dumps(results, indent=2))
    print(f"details: {run / 'stress.json'}")


if __name__ == "__main__":
    main()
