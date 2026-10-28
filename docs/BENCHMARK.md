# Benchmark notes

Programs: 40 COBOL programs from IBM Project CodeNet, one stdin sample each.
Model: Nemotron Ultra via the free NVIDIA API, up to 3 fix rounds.

| Result | Value |
|---|---|
| Equivalent to GnuCOBOL output | 39 / 40 |
| Failed | p03845 (mismatch, then timeout, then 20,000-token cap) |
| Tokens (in / out) | 101,575 / 158,013 |
| Wall time | 2,327 s (2 jobs in parallel) |
| Cost | ~$0.58 at list prices (estimate, not an invoice) |

**What this does and does not show.** Equivalence is checked by running both
programs on the sample input and comparing output exactly. That is differential
testing, not a proof. One input per program is weak evidence; `backend/stress.py`
adds model-generated extra inputs (ground truth still comes from GnuCOBOL).

CodeNet files are not redistributed in this repo; see `benchmark/` to rebuild.
