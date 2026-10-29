#!/usr/bin/env python3
"""Parity web server (standard library only).

  GET  /                 the single-page UI
  GET  /api/samples      bundled sample COBOL programs
  GET  /api/config       tiers and model ids in use
  POST /api/translate    JSON {cobol, inputs[], tiers[], fix_rounds} -> Server-Sent Events
  GET  /api/spend        spend so far vs the budget (answers on localhost only)
  POST /api/chat         JSON {messages[], context{}, think} -> Server-Sent Events (assistant)

Run:  python server.py            (PORT env var, default 8000)
Needs the same .env as parity.py, plus cobc and a JDK on PATH.
"""
import json
import ctypes
import ipaddress
import math
import os
import queue
import re
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import assistant
import parity

parity.SAFE = True
ISOLATION_STATUS = "failed"
HERE = Path(__file__).resolve().parent.parent
FRONTEND = HERE / "frontend"
EXAMPLES = HERE / "examples"
MAX_COBOL = 20_000
MAX_INPUTS = 5
MAX_INPUT_LEN = 2_000
DAILY_CAP = int(os.environ.get("PARITY_DAILY_CAP", "300"))
IP_DAILY_CAP = int(os.environ.get("PARITY_IP_DAILY_CAP", "40"))
TRUST_PROXY = os.environ.get("PARITY_TRUST_PROXY") == "1"
MAX_BODY = 200_000
DEFAULT_TIERS = [t for t in os.environ.get("PARITY_UI_TIERS", "ultra").split(",") if t]
TIME_BUDGET = int(os.environ.get("PARITY_TIME_BUDGET", "600"))  # seconds for one whole run
BIG_LINES = int(os.environ.get("PARITY_BIG_LINES", "120"))       # longer programs skip the small model
FIX_ROUNDS = 4                                                    # fix attempts per model (internal)
CHAT_CAP = int(os.environ.get("PARITY_CHAT_CAP", "400"))            # chat messages per day
chat_slots = threading.BoundedSemaphore(int(os.environ.get("PARITY_MAX_CHATS", "4")))
_chat_count = {"day": time.strftime("%Y-%m-%d"), "n": 0}
_ip_counts = {"day": time.strftime("%Y-%m-%d"), "counts": {}}
_ip_count_lock = threading.Lock()
# Spending guard. PARITY_BUDGET_USD is a hard stop for the whole server (0 = no limit): once the total
# estimated cost reaches it, new translations and chat messages are refused. The total is kept in
# results/spend.json so a restart does not reset it. Prices are USD per million tokens; set them to the
# model's real price on your provider (PARITY_PRICE_IN / PARITY_PRICE_OUT).
BUDGET_USD = float(os.environ.get("PARITY_BUDGET_USD", "0") or 0)
PRICE_IN = float(os.environ.get("PARITY_PRICE_IN", parity.PRICES["ultra"]["in"] or 0))
PRICE_OUT = float(os.environ.get("PARITY_PRICE_OUT", parity.PRICES["ultra"]["out"]))
SPEND_FILE = HERE / "results" / "spend.json"
_spend_lock = threading.Lock()


def isolation_self_test(run_child=None):
    """Verify an untrusted child cannot read this process's environment."""
    if not sys.platform.startswith("linux"):
        return "ok"
    run_child = run_child or parity.run
    try:
        with tempfile.TemporaryDirectory() as td:
            rc, _out, err = run_child(["cat", f"/proc/{os.getpid()}/environ"],
                                      timeout=5, tmpdir=td)
    except Exception:
        return "failed"
    if rc == 0:
        return "failed"
    return "ok" if "permission denied" in str(err).lower() else "failed"


def initialize_isolation():
    """Harden proc access and cache the child-read self-test at server startup."""
    global ISOLATION_STATUS
    if not parity.SAFE or not sys.platform.startswith("linux"):
        ISOLATION_STATUS = "ok"
        return ISOLATION_STATUS
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        prctl = libc.prctl
        prctl.restype = ctypes.c_int
        dumpable_disabled = prctl(ctypes.c_int(4), ctypes.c_ulong(0), ctypes.c_ulong(0),
                                  ctypes.c_ulong(0), ctypes.c_ulong(0)) == 0
    except (AttributeError, OSError):
        dumpable_disabled = False
    ISOLATION_STATUS = isolation_self_test() if dumpable_disabled else "failed"
    if ISOLATION_STATUS == "ok":
        print("isolation check passed", flush=True)
    else:
        print("ISOLATION CHECK FAILED: generated code could read the server environment", flush=True)
    return ISOLATION_STATUS


def _load_spend(path):
    empty = {"usd": 0.0, "tin": 0, "tout": 0, "calls": 0}
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return empty, None
    except OSError as exc:
        return empty, exc
    try:
        d = json.loads(raw)
        if not isinstance(d, dict):
            raise ValueError("spend data is not an object")
        data = {"usd": float(d["usd"]), "tin": int(d["tin"]), "tout": int(d["tout"]),
                "calls": int(d["calls"])}
        if any(v < 0 for v in data.values()) or not math.isfinite(data["usd"]):
            raise ValueError("negative spend value")
        return data, None
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        return empty, exc


_spend, _spend_error = _load_spend(SPEND_FILE)


def add_spend(tin, tout):
    global _spend_error
    tin, tout = max(1, int(tin or 0)), max(1, int(tout or 0))
    with _spend_lock:
        if _spend_error:
            return False
        _spend["usd"] += (tin * PRICE_IN + tout * PRICE_OUT) / 1e6
        _spend["tin"] += tin
        _spend["tout"] += tout
        _spend["calls"] += 1
        if not _persist_spend(_spend):
            _spend_error = OSError("could not persist spend guard")
            return False
        print(f"[spend] ${_spend['usd']:.4f}" + (f" of ${BUDGET_USD:g}" if BUDGET_USD else " (no limit set)"), flush=True)
        return True


def _persist_spend(data):
    temp_path = None
    try:
        SPEND_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix=".spend-", dir=SPEND_FILE.parent)
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, SPEND_FILE)
        return True
    except OSError:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        return False


def over_budget():
    with _spend_lock:
        return bool(_spend_error) or (BUDGET_USD > 0 and _spend["usd"] >= BUDGET_USD)


BUDGET_MSG = "The demo's AI budget is used up, so it is paused. Please contact the author."
slots = threading.BoundedSemaphore(int(os.environ.get("PARITY_MAX_JOBS", "2")))
_count = {"day": time.strftime("%Y-%m-%d"), "n": 0}
_count_lock = threading.Lock()
CALL_RE = re.compile(r"\bCALL\b", re.I)


def _has_copy(cobol):
    for raw in cobol.splitlines():
        line = raw
        if len(raw) >= 7 and (not raw[:6].strip() or raw[:6].isdigit()):
            if raw[6] in "*/":
                continue
            line = raw[7:]
        line = line.split("*>", 1)[0]
        if re.match(r"^\s*COPY\s+[A-Za-z0-9_-]+\b", line, re.I):
            return True
    return False


def parse_content_length(value):
    if value is None:
        return None, "Content-Length is required."
    try:
        length = int(value.strip())
    except (AttributeError, ValueError):
        return None, "Content-Length must be an integer."
    if length < 0:
        return None, "Content-Length cannot be negative."
    if length > MAX_BODY:
        return None, "Request too large."
    return length, None


def over_cap():
    with _count_lock:
        today = time.strftime("%Y-%m-%d")
        if _count["day"] != today:
            _count.update(day=today, n=0)
        _count["n"] += 1
        return _count["n"] > DAILY_CAP


def over_chat_cap():
    with _count_lock:
        today = time.strftime("%Y-%m-%d")
        if _chat_count["day"] != today:
            _chat_count.update(day=today, n=0)
        _chat_count["n"] += 1
        return _chat_count["n"] > CHAT_CAP


def over_ip_cap(ip):
    with _ip_count_lock:
        today = time.strftime("%Y-%m-%d")
        if _ip_counts["day"] != today:
            _ip_counts.update(day=today, counts={})
        count = _ip_counts["counts"].get(ip, 0) + 1
        _ip_counts["counts"][ip] = count
        return count > IP_DAILY_CAP


def validate(body):
    cobol = body.get("cobol")
    if not isinstance(cobol, str) or not cobol.strip():
        return None, "Paste a COBOL program first."
    if len(cobol) > MAX_COBOL:
        return None, f"Program too long (max {MAX_COBOL} characters)."
    if CALL_RE.search(cobol):
        return None, ("The hosted demo does not run programs that use CALL "
                      "(it can reach the operating system).")
    if _has_copy(cobol):
        return None, "COPY statements must be expanded before upload."
    inputs = body.get("inputs") or [""]
    if not isinstance(inputs, list) or len(inputs) > MAX_INPUTS:
        return None, f"At most {MAX_INPUTS} test inputs."
    if any(len(str(i)) > MAX_INPUT_LEN for i in inputs):
        return None, f"Each test input must be at most {MAX_INPUT_LEN} characters."
    inputs = [str(i) for i in inputs]
    tiers = DEFAULT_TIERS  # the UI uses one model; the cascade lives in the benchmark script
    if not tiers:
        return None, "No valid model tier selected."
    return {"cobol": cobol, "inputs": inputs, "tiers": tiers, "fix_rounds": FIX_ROUNDS}, None


def run_job(req, emit):
    cobol, tests, tiers = req["cobol"], req["inputs"], req["tiers"]
    t0 = time.time()
    deadline = t0 + TIME_BUDGET
    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        cob = wd / "prog.cob"
        cob.write_text(cobol)
        emit(type="stage", text="Compiling and running the COBOL program (ground truth)")
        expected, fmt, err = parity.ground_truth(cob, tests, wd)
        if err:
            emit(type="error", text=err)
            return
        emit(type="oracle", format=fmt, outputs=expected, stdins=tests)
        n_lines = len(cobol.splitlines())
        if len(tiers) > 1 and n_lines > BIG_LINES:
            tiers = tiers[-1:]
            emit(type="note", text=f"{n_lines} lines: too long for the small model, "
                                   "sending it straight to the large model")
        totals = {"tin": 0, "tout": 0, "cost_usd": 0.0}
        per_tier = []
        solved_by = None
        java = None
        stopped = None
        for ti, tier in enumerate(tiers):
            jd = wd / f"java_{tier}"
            jd.mkdir()
            emit(type="tier_start", tier=tier, model=parity.model_for(tier))
            # the small model gets one fix attempt: if it repeats a mistake, escalate rather than wait
            rec = parity.solve_tier(tier, cobol, tests, expected, jd,
                                    1 if tier == "nano" else req["fix_rounds"],
                                    last_tier=(ti == len(tiers) - 1), on_event=emit,
                                    deadline=deadline)
            cost = parity.cost_usd(tier, rec["tin"], rec["tout"])
            per_tier.append({"tier": tier, "tin": rec["tin"], "tout": rec["tout"],
                             "secs": round(rec["secs"], 1), "cost_usd": cost,
                             "solved": rec["solved"]})
            totals["tin"] += rec["tin"]
            totals["tout"] += rec["tout"]
            totals["cost_usd"] += cost
            if rec["solved"]:
                solved_by, java = tier, rec["java"]
                break
            if rec.get("stopped"):
                stopped = rec["stopped"]
                break
        emit(type="done", solved=solved_by is not None, solved_by=solved_by, java=java,
             per_tier=per_tier, totals=totals, secs=round(time.time() - t0, 1),
             n_tests=len(tests), stopped=stopped, budget=TIME_BUDGET)


class Handler(BaseHTTPRequestHandler):
    server_version = "Parity/2"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj), "application/json")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/health":
            self._json(200, {"isolation": ISOLATION_STATUS})
        elif path in ("/", "/index.html"):
            self._send(200, (FRONTEND / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/samples":
            out = []
            for f in sorted((EXAMPLES).glob("*.cob")):
                out.append({"name": f.stem, "cobol": f.read_text(errors="replace"),
                            "inputs": [""]})
            self._json(200, out)
        elif path == "/api/spend":
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                return self._send(404, "not found", "text/plain")
            with _spend_lock:
                self._json(200, {"spent_usd": round(_spend["usd"], 4), "limit_usd": BUDGET_USD or None,
                                 "tokens_in": _spend["tin"], "tokens_out": _spend["tout"], "calls": _spend["calls"],
                                 "price_in_per_m": PRICE_IN, "price_out_per_m": PRICE_OUT})
        elif path == "/api/config":
            self._json(200, {"tiers": DEFAULT_TIERS,
                             "models": {t: parity.model_for(t) for t in parity.DEFAULT_MODELS},
                             "max_inputs": MAX_INPUTS})
        else:
            self._send(404, "not found", "text/plain")

    def _read_body(self):
        length, error = parse_content_length(self.headers.get("Content-Length"))
        if error:
            self._json(413 if error == "Request too large." else 400, {"error": error})
            return None
        try:
            deadline = time.monotonic() + 10
            chunks = bytearray()
            while len(chunks) < length:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise socket.timeout
                self.connection.settimeout(remaining)
                chunk = self.rfile.read1(min(65536, length - len(chunks)))
                if not chunk:
                    break
                chunks.extend(chunk)
            raw = bytes(chunks)
            if len(raw) != length:
                self._json(400, {"error": "incomplete request body"})
                return None
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                self._json(400, {"error": "request body must be a JSON object"})
                return None
            return body
        except socket.timeout:
            self._json(408, {"error": "request body timed out"})
            return None
        except Exception:
            self._json(400, {"error": "bad JSON"})
            return None
        finally:
            try:
                self.connection.settimeout(None)
            except OSError:
                pass

    def _sse(self, job, sem):
        """Run job(emit) on a worker thread and stream what it emits; sem is released at the end."""
        q = queue.Queue()

        def worker():
            try:
                def emit(ev=None, **kw):
                    ev = ev if ev is not None else kw
                    t = ev.get("type")
                    if t in ("model_reply", "usage") or (t == "done" and "tin" in ev):
                        if not add_spend(ev.get("tin"), ev.get("tout")) or over_budget():
                            raise RuntimeError(BUDGET_MSG)
                    q.put(ev)

                job(emit)
            except Exception as e:  # noqa: BLE001
                q.put({"type": "error", "text": f"{type(e).__name__}: {str(e)[:200]}"})
            finally:
                sem.release()
                q.put(None)

        threading.Thread(target=worker, daemon=True).start()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            while True:
                try:
                    item = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if item is None:
                    break
                self.wfile.write(b"data: " + json.dumps(item).encode() + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        if self.path not in ("/api/translate", "/api/chat"):
            return self._send(404, "not found", "text/plain")
        if parity.SAFE and ISOLATION_STATUS != "ok":
            return self._json(503, {"error": "Server isolation check failed, translation disabled"})
        body = self._read_body()
        if body is None:
            return
        if over_budget():
            return self._json(429, {"error": BUDGET_MSG})
        if self.path == "/api/chat":
            msgs, err = assistant.build_messages(body)
            if err:
                return self._json(400, {"error": err})
            if over_chat_cap():
                return self._json(429, {"error": "Daily chat limit reached. Try again tomorrow."})
            if not chat_slots.acquire(blocking=False):
                return self._json(429, {"error": "The assistant is busy. Try again in a moment."})
            return self._sse(lambda emit: assistant.stream(body, emit), chat_slots)
        req, err = validate(body)
        if err:
            return self._json(400, {"error": err})
        if not slots.acquire(blocking=False):
            return self._json(429, {"error": "The demo is busy. Try again in a minute."})
        if over_cap():
            slots.release()
            return self._json(429, {"error": "Daily demo limit reached. Try again tomorrow."})
        peer = self.client_address[0]
        if TRUST_PROXY:
            forwarded = self.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
            try:
                peer = ipaddress.ip_address(forwarded).compressed if forwarded else peer
            except ValueError:
                pass
        if over_ip_cap(peer):
            slots.release()
            return self._json(429, {"error": "Daily run limit reached for this IP."})
        self._sse(lambda emit: run_job(req, emit), slots)


def main():
    port = int(os.environ.get("PORT", "8000"))
    initialize_isolation()
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Parity UI on http://localhost:{port}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
