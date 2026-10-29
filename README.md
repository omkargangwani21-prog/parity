# Parity

An AI agent that translates COBOL to Java and **checks the result by running both**.
GnuCOBOL's output is the answer key. The agent loops: translate, run, diff, fix.

Built for the Nebius x NVIDIA Global AI Hackathon (Coding & Agentic Engineering).

**Live demo:** https://parity-6ypb.onrender.com (free hosting: the first load after idle can take 30-60 seconds to wake up; model calls stop when the demo budget is used up).

## Why
COBOL still runs a large share of banking and government batch work, and the people
who can read it are retiring. Translating it is the easy part. Knowing the Java
behaves the same is the hard part. Parity does not ask you to trust the model: it
executes the original, executes the translation, and compares the output.

## How it works
1. Paste COBOL (or open a folder of programs) in the web UI.
2. **Nemotron 3 Ultra** (via Nebius Token Factory) writes Java.
3. Parity compiles and runs both programs on the same inputs and compares output exactly.
4. On a mismatch, an aligned line-level diff goes back to the model for another try (up to 5 attempts; the last rounds enable the model's thinking mode).
5. Verdict: **equivalent on the tested inputs**, or **not matched**.

An empty comparison is never a pass. This is differential testing, not formal verification:
passing means the outputs matched on the inputs that were run, nothing more.

## Features
- Folder mode: open a folder, **Convert All**, download the results as a ZIP.
- COPY statements are filled in from copybooks in the same folder. Fixed and free format both work.
- Test inputs per program: `NAME.in`, `NAME.2.in`, ... next to the `.cob` file.
- An assistant chat that can explain a run, a diff, or a COBOL construct.
- Not supported: `CALL` (programs that call other programs are skipped, and say so).

## Results (details in [docs/BENCHMARK.md](docs/BENCHMARK.md))
- 40 real COBOL programs from IBM Project CodeNet: **38 / 40** matched GnuCOBOL on the sample input (about $1.17, 642 s, Nemotron 3 Ultra on Nebius).
- Extra model-generated inputs on the 38 that passed: **137 / 138** inputs matched; 1 program (p03309) differed on one input.
- The 2 that did not match are reported as failures, not hidden.

## Layout
```
parity/
├── backend/      parity.py (agent + CLI), server.py (web server), assistant.py, stress.py
├── frontend/     index.html (web UI)
├── benchmark/    scripts to rebuild the CodeNet benchmark and stress test
├── tests/        unit tests
├── docs/         benchmark notes
├── results/      run output (git-ignored)
├── Dockerfile  requirements.txt  run.sh  .env.example
```

## Run
Needs Python 3.10+, GnuCOBOL (`cobc`), and a JDK.
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # add your Nebius Token Factory API key
./run.sh                  # http://localhost:8000
```
Docker: `docker build -t parity . && docker run -p 8000:8000 --env-file .env parity`
CLI: `./run.sh bench <folder-with-.cob-files> --tiers ultra --fix-rounds 4`
Tests: `python -m unittest discover -s tests`

## Model and API
Nemotron 3 Ultra (`nvidia/Nemotron-3-Ultra-550b-a55b`) through the OpenAI-compatible
Nebius Token Factory endpoint (`https://api.tokenfactory.nebius.com/v1/`). Settings are in `.env.example`.

## Safety notes
Parity runs generated Java and the submitted COBOL on the server. Child processes get a
scrubbed environment, CPU/memory/process/file-size limits, bounded output, and (in Docker)
an unprivileged user. A spend guard stops model calls at a daily budget, and there is a
per-IP daily limit. This is defense in depth for a demo, not a hardened multi-tenant
sandbox. Do not expose it to untrusted users without stronger isolation.

## Known limits
- One run can differ from the next: results from a language model vary. Larger programs
  sometimes need all 5 attempts.
- Numeric edge cases (implied decimals, truncation) were the main failure source we saw.
- Output equivalence on sampled inputs says nothing about inputs not tried.

## License
MIT
