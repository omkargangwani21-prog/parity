#!/usr/bin/env python3
"""Extract the COBOL part of Project CodeNet in ONE streaming pass.

Usage:
  python extract_codenet.py <Project_CodeNet.tar.gz> <cobol_files.txt> <out_dir>

For every problem that has COBOL submissions it writes:
  <out>/<pid>/cobol/<sid>.cob        the submissions
  <out>/<pid>/io/<...>               sample input/output files (derived/input_output)
  <out>/<pid>/meta/<pid>.csv         per-submission status (Accepted, ...)
  <out>/<pid>/<pid>.html             problem statement

The archive is untrusted: nothing is extracted with extractall; every output path
is built from regex-validated parts only.
"""
import re
import sys
import tarfile
from collections import Counter
from pathlib import Path

if len(sys.argv) != 4:
    sys.exit(__doc__)

src, listing, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
pids = set(re.findall(r"/data/(p\d{5})/COBOL/", listing.read_text()))
print(f"{len(pids)} problems have COBOL submissions", file=sys.stderr)

RE_COBOL = re.compile(r"/data/(p\d{5})/COBOL/(s\d{9}\.[A-Za-z0-9]+)$")
RE_IO = re.compile(r"/derived/input_output/(?:data/)?(p\d{5})/([A-Za-z0-9._/-]+)$")
RE_META = re.compile(r"/metadata/(p\d{5})\.csv$")
RE_HTML = re.compile(r"/problem_descriptions/(p\d{5})\.html$")

counts = Counter()
io_examples = []


def write(dest, data):
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)


with tarfile.open(src, "r|gz") as tf:
    for n, m in enumerate(tf, 1):
        if n % 200000 == 0:
            print(f"  scanned {n} entries, kept {sum(counts.values())}", file=sys.stderr)
        if not m.isfile():
            continue
        name = m.name
        dest = None
        kind = None
        mo = RE_COBOL.search(name)
        if mo and mo.group(1) in pids:
            kind, dest = "cobol", out / mo.group(1) / "cobol" / mo.group(2)
        else:
            mo = RE_IO.search(name)
            if mo and mo.group(1) in pids and ".." not in mo.group(2):
                kind, dest = "io", out / mo.group(1) / "io" / mo.group(2)
                if len(io_examples) < 8:
                    io_examples.append(name)
            else:
                mo = RE_META.search(name)
                if mo and mo.group(1) in pids:
                    kind, dest = "meta", out / mo.group(1) / "meta" / f"{mo.group(1)}.csv"
                else:
                    mo = RE_HTML.search(name)
                    if mo and mo.group(1) in pids:
                        kind, dest = "html", out / mo.group(1) / f"{mo.group(1)}.html"
        if dest is None:
            continue
        f = tf.extractfile(m)
        if f is None:
            continue
        write(dest, f.read())
        counts[kind] += 1

print("\nDONE:", dict(counts))
print("Example sample-I/O paths inside the archive:")
for e in io_examples:
    print("  ", e)
