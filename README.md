# Parity

An AI agent that translates COBOL to Java and **checks the result by running both**.
GnuCOBOL's output is the answer key. The agent loops: translate, run, diff, fix.

Built for the Nebius x NVIDIA Global AI Hackathon (Coding & Agentic Engineering) using Nemotron.

## How it works
1. Paste COBOL in the web UI.
2. Nemotron writes Java.
3. Parity compiles and runs both, compares output exactly.
4. On mismatch, the line-level diff goes back to the model for another try.
5. Verdict: equivalent on the tested input, or not.

This is differential testing, not formal verification.

## Layout
```
parity/
├── backend/      parity.py (agent + CLI), server.py (web server), stress.py
├── frontend/     index.html (web UI)
├── benchmark/    scripts to rebuild the CodeNet benchmark
├── docs/         benchmark notes
├── results/      run output (git-ignored)
├── Dockerfile  requirements.txt  run.sh  .env.example
```

## Run
Needs Python 3.10+, GnuCOBOL (`cobc`), and a JDK.
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # add your API key
./run.sh                  # http://localhost:8000
```
CLI: `./run.sh bench <folder-with-.cob-files> --tiers ultra --fix-rounds 3`
Stress test: `python backend/stress.py results/<run> <bench-dir> --extra 4`

Docker: `docker build -t parity . && docker run -p 8000:8000 --env-file .env parity`

## Results
See [docs/BENCHMARK.md](docs/BENCHMARK.md).

## License
MIT