# Benchmark notes

## Sample-input benchmark
Programs: 40 COBOL programs from IBM Project CodeNet, one stdin sample each.
Model: Nemotron 3 Ultra on Nebius Token Factory, up to 4 fix rounds.

| Result | Value |
|---|---|
| Valid translations (compile and run) | 40 / 40 |
| Equivalent to GnuCOBOL output | 38 / 40 |
| Failed | p03501, p03845 |
| Cost | $1.1731 at list prices ($1.00 in / $0.12 cached / $3.00 out per 1M tokens), from the run log |
| Wall time | 642 s |

## Stress test (extra inputs)
For the 38 programs above, the model proposed extra inputs; ground truth still comes
from GnuCOBOL. 137 of 138 inputs matched. One program, p03309, differed on one input
(a truncation case). Failures stay in the numbers.

## What this does and does not show
Equivalence is checked by running both programs and comparing output exactly.
That is differential testing, not a proof. A handful of inputs per program is limited
evidence, and model output varies between runs, so these figures describe the runs
above and should not be read as a guaranteed rate.

CodeNet files are not redistributed here; `benchmark/build_bench.py` and
`benchmark/extract_codenet.py` rebuild the set from the public dataset.
