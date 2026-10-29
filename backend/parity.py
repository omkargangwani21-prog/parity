#!/usr/bin/env python3
"""Parity v2: COBOL -> Java with behavioral-equivalence testing and a model cascade.

For every COBOL program:
  1. Compile + run it with GnuCOBOL on each test input -> the reference ("oracle") output.
  2. Ask a Nemotron model to translate it to Java; compile + run the Java on the same inputs.
  3. If outputs differ, show the model the difference and let it fix (up to --fix-rounds).
  4. If a tier cannot solve it, escalate to the next tier (nano -> super -> ultra).

Test inputs for  foo.cob  are the files  foo.1.in, foo.2.in, ...  (none = one empty input).

Usage:
  python parity.py run bench                      # whole folder, cascade nano,super,ultra
  python parity.py run bench --tiers ultra        # ultra only (cost baseline)
  python parity.py run bench --limit 5 --jobs 2   # quick smoke run
  python parity.py run samples/payroll.cob        # a single file
Results: results/<timestamp>/summary.json and results/<timestamp>/cases/<name>.json
"""
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # project root (holds .env and results/)


def load_env(path=ROOT / ".env"):
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


if os.environ.get("PARITY_SKIP_DOTENV") != "1":
    load_env()

COBC = os.environ.get("COBC", "cobc")
MAX_TOKENS = int(os.environ.get("PARITY_MAX_TOKENS", "20000"))

# Defaults are the NVIDIA build.nvidia.com ids; override in .env for Nebius Token Factory.
DEFAULT_MODELS = {
    "nano": "nvidia/nemotron-nano-3-30b-a3b",
    "super": "nvidia/nemotron-3-super-120b-a12b",
    "ultra": "nvidia/nemotron-3-ultra-550b-a55b",
}
# USD per 1M tokens: Token Factory list prices read from benchlm.ai on 2026-10-20.
# The nano INPUT price was not published there (None), so nano cost is a lower bound.
PRICES = {
    "nano": {"in": None, "out": 0.24},
    "super": {"in": 0.30, "out": 0.90},
    "ultra": {"in": 1.00, "out": 3.00},
}


def model_for(tier):
    return os.environ.get(f"PARITY_MODEL_{tier.upper()}", DEFAULT_MODELS[tier])


def cost_usd(tier, tin, tout):
    p = PRICES[tier]
    return (tin * (p["in"] or 0) + tout * p["out"]) / 1e6


SYSTEM = (
    "You translate COBOL programs to Java 17. Reply with exactly one file, Main.java, containing "
    "`public class Main` with a main method, inside a single ```java code block and nothing else. "
    "Write no comments at all and no reasoning: the code is your final answer. Use only the Java "
    "standard library.\n\n"
    "For the same stdin, the Java program's stdout must match the COBOL program's stdout EXACTLY, "
    "character for character. COBOL rules to follow:\n"
    "1. Read input like COBOL ACCEPT: one line per ACCEPT; fixed-width fields truncate or pad.\n"
    "2. Use BigDecimal for every numeric field that has decimals. Each stored result has the scale "
    "of its PIC clause (V99 means 2 decimals).\n"
    "3. COMPUTE, ADD, SUBTRACT, MULTIPLY and DIVIDE WITHOUT ROUNDED cut off extra decimals toward "
    "zero when they store (RoundingMode.DOWN). WITH ROUNDED use RoundingMode.HALF_UP. Never round "
    "at any other point. Inside one expression keep full precision (divide with a large scale such "
    "as 30) and apply the field's scale only when storing the result.\n"
    "4. If a stored value has more integer digits than its PIC allows, the high-order digits are "
    "dropped (value mod 10^digits). This also applies to numbers read by ACCEPT or UNSTRING and to "
    "COMP / BINARY fields: reading '15' into PIC 9(1) stores 5, and 9999999999 into PIC 9(8) stores "
    "99999999. Apply the cut when you parse the input, not only after arithmetic.\n"
    "5. An unsigned field (no S in the PIC) never holds a minus sign: it keeps the absolute value.\n"
    "6. DISPLAY of a plain numeric field prints it zero-padded to its full PIC width, with no "
    "decimal point for an implied V decimal.\n"
    "7. MOVE to a numeric-edited field (a PIC with Z , . 9 and similar) produces a string exactly "
    "as long as the picture. Write one static helper with this algorithm. Build the digit string: "
    "the value's digits without the decimal point, cut or zero-padded on the left to exactly the "
    "number of Z and 9 positions in the picture. Set started = false. Walk the picture characters "
    "LEFT TO RIGHT, appending to a StringBuilder in normal order (never build it backwards, never "
    "reverse it): for 9, append the next digit and set started = true; for Z, take the next digit: "
    "if it is '0' and started is false append a space, else append the digit and set started = true; "
    "for a comma, append ',' if started, otherwise a space; for a period, append '.' and set "
    "started = true. Never use DecimalFormat, String.format widths or trim for this: the spaces on "
    "the left are part of the output.\n"
    "8. Alphanumeric fields (PIC X(n)) have fixed length: pad with spaces on the right, or cut off "
    "on the right.\n"
    "9. EVALUATE, PERFORM VARYING ... UNTIL, PERFORM n TIMES, INSPECT, STRING and OCCURS tables "
    "follow COBOL semantics; tables are 1-based.\n"
    "10. Put every helper as a static method of Main, never nested inside main.\n"
    "11. Storage layout: every digit, letter, 9, X or Z position in a PIC takes exactly one "
    "character; V (implied decimal point) and an S without SIGN SEPARATE take none, so PIC 9(7)V99 "
    "is 9 characters and PIC 9V999 is 4. When VALUE strings are redefined as a table or group, "
    "each field is the next consecutive slice of the concatenated text: compute every offset as the "
    "sum of the widths of the fields before it (in code, from constants) and read each field with "
    "exactly its own width.\n"
    "12. Table subscripts: DIC(J) uses a 1-based COBOL subscript. Allocate every table one element "
    "larger than its OCCURS maximum and use the COBOL subscript directly (index 0 unused). Never use "
    "a COBOL subscript as a 0-based list index; that is an off-by-one that crashes on small inputs.\n"
    "13. Alphanumeric DISPLAY prints every character of the field, including trailing spaces, unless "
    "the program trims it. FUNCTION TRIM(x) removes leading and trailing spaces. FUNCTION "
    "STORED-CHAR-LENGTH(x) is the length of x without its trailing spaces, so DISPLAY "
    "x(1:FUNCTION STORED-CHAR-LENGTH(x)) prints x with no trailing spaces. FUNCTION LENGTH(x) is the "
    "declared size. Translate each of these exactly, so a one-character value prints with no padding.\n"
    "14. DISPLAY of a figurative numeric constant prints its numeric character, not its COBOL name: "
    "DISPLAY ZERO prints 0 (never the word ZERO). ZERO, ZEROES and ZEROES are numeric zero values; "
    "preserve the COBOL statement's display width and formatting.\n"
    "15. INSPECT ... TALLYING adds to the counter's CURRENT value; it does not set it to zero first. "
    "If the COBOL does not MOVE 0 (or INITIALIZE) to the counter before an INSPECT, the count keeps "
    "accumulating every time the INSPECT runs (for example inside a PERFORM VARYING loop over several "
    "records), so the Java must NOT reset the counter either. Reset the Java counter only at the points "
    "where the COBOL resets it.\n"
    "16. The V in a PICTURE is an implied decimal point: it marks where the decimal point sits and takes "
    "no storage. It never divides or multiplies a value. MOVE 100000.00 (or VALUE 100000.00) into "
    "PIC 9(7)V99 stores 100000.00. A number read with ACCEPT or FUNCTION NUMVAL that has no decimal point, "
    "for example 1200000, stored into PIC 9(9)V99 is 1200000.00, and 100 is 100.00. Never shift the decimal "
    "point of a value that is already a number. Only raw digit characters taken from an alphanumeric "
    "(PIC X) item, for example through a REDEFINES table of VALUE strings, are read with the V position: "
    "the digits 000125050 viewed as PIC 9(7)V99 mean 1250.50."
)

_client = None
_client_lock = threading.Lock()


def estimate_tokens(text):
    """Conservative local estimate used only when provider usage is unavailable."""
    return max(1, (len(text) + 3) // 4)


def client():
    global _client
    with _client_lock:
        if _client is None:
            import httpx
            from openai import OpenAI

            _client = OpenAI(
                base_url=os.environ["NEBIUS_BASE_URL"],
                api_key=os.environ["NEBIUS_API_KEY"],
                # No hidden SDK retries (they silently multiplied waits). The read timeout is
                # per chunk of a streamed reply, so a long answer is fine but a dead one is not.
                max_retries=0,
                timeout=httpx.Timeout(connect=15, read=120, write=30, pool=30),
            )
        return _client


CALL_SECS = int(os.environ.get("PARITY_CALL_SECS", "480"))   # wall-clock limit for one model reply
# Reasoning control (measured on the free NVIDIA API: the small model went from ~2,000 hidden
# reasoning chunks / 27 s to 0 / 0.8 s with thinking off). Default "auto": attempts 1-2 do not think
# (fast), the last tier thinks from attempt 3 (a no-think run repeated the same COBOL rounding and
# edited-picture mistakes 5 times). "on" / "off" force it.
THINK_MODE = os.environ.get("PARITY_THINK", "auto").lower()


def chat(model, messages, max_tokens=None, on_progress=None, think=False):
    """Streamed chat call. Returns text, finish ('stop' | 'length' | 'timeout'), token counts."""
    last = None
    for attempt in range(2):
        if attempt:
            time.sleep(4)
        t0 = time.time()
        parts, n, finish, usage, reasoning_chars = [], 0, None, None, 0
        kw = dict(model=model, messages=messages, temperature=0.2,
                  max_tokens=max_tokens or MAX_TOKENS, stream=True)
        if attempt == 0:  # the retry uses the plainest request in case an option was rejected
            kw["stream_options"] = {"include_usage": True}
            if not think:
                kw["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
        try:
            stream = client().chat.completions.create(**kw)
            last_emit = 0.0
            for chunk in stream:
                if getattr(chunk, "usage", None):
                    usage = chunk.usage
                if not chunk.choices:
                    continue
                ch = chunk.choices[0]
                d = ch.delta
                piece = getattr(d, "content", None) or ""
                reason = getattr(d, "reasoning_content", None) or getattr(d, "reasoning", None) or ""
                if piece:
                    parts.append(piece)
                reasoning_chars += len(reason)
                if piece or reason:
                    n += 1
                if ch.finish_reason:
                    finish = ch.finish_reason
                now = time.time()
                if on_progress and now - last_emit >= 1.0:
                    last_emit = now
                    on_progress(n, now - t0)
                if now - t0 > CALL_SECS:
                    finish = "timeout"
                    try:
                        stream.close()
                    except Exception:  # noqa: BLE001
                        pass
                    break
            prompt_tokens = getattr(usage, "prompt_tokens", None) if usage else None
            completion_tokens = getattr(usage, "completion_tokens", None) if usage else None
            estimated = prompt_tokens is None or completion_tokens is None
            prompt_estimate = estimate_tokens("\n".join(str(m.get("content", "")) for m in messages))
            output_estimate = max(1, (sum(map(len, parts)) + reasoning_chars + 3) // 4)
            return {
                "text": "".join(parts),
                "finish": finish or "stop",
                "tin": prompt_tokens if prompt_tokens and prompt_tokens > 0 else prompt_estimate,
                "tout": completion_tokens if completion_tokens and completion_tokens > 0 else output_estimate,
                "usage_estimated": estimated or not prompt_tokens or not completion_tokens,
                "secs": time.time() - t0,
            }
        except Exception as e:  # noqa: BLE001 - one retry on transport/API errors
            last = e
            if parts or reasoning_chars:  # partial reply still has billable usage
                return {"text": "".join(parts), "finish": "length",
                        "tin": estimate_tokens("\n".join(str(m.get("content", "")) for m in messages)),
                        "tout": max(1, (sum(map(len, parts)) + reasoning_chars + 3) // 4),
                        "usage_estimated": True,
                        "secs": time.time() - t0}
    raise last


SAFE = False  # the web server turns this on: resource limits + blocked APIs
BANNED_JAVA = re.compile(
    r"ProcessBuilder|Runtime\s*\.\s*getRuntime|java\.net|java\.nio\.file|java\.io\.File"
    r"|FileReader|FileWriter|URLClassLoader|System\s*\.\s*(load|setSecurityManager)|java\.lang\.reflect")


_CHILD_OUTPUT_LIMIT = 1 << 20
_COB_CHILD_ENV = ("COB_CONFIG_DIR", "COB_COPY_DIR", "COB_LIBRARY_PATH", "COB_PRE_LOAD")


def child_env(tmpdir):
    """Expose only toolchain settings to untrusted child processes."""
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(Path(tmpdir).resolve())}
    for key in ("LANG", "JAVA_HOME", *_COB_CHILD_ENV):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def _prepare_child_dir(tmpdir):
    tmpdir = Path(tmpdir)
    tmpdir.mkdir(parents=True, exist_ok=True)
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        os.chown(tmpdir, 65534, 65534)
        os.chmod(tmpdir, 0o700)
    return tmpdir


def _child_preexec(address_space=1 << 30):
    if os.name != "posix":
        return None
    import resource

    def setup():
        resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
        resource.setrlimit(resource.RLIMIT_AS, (address_space, address_space))
        resource.setrlimit(resource.RLIMIT_NPROC, (256, 256))
        resource.setrlimit(resource.RLIMIT_FSIZE, (5 << 20, 5 << 20))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if os.geteuid() == 0:
            os.setgroups([])
            os.setgid(65534)
            os.setuid(65534)
        os.umask(0o077)
    return setup


def run(cmd, stdin=None, cwd=None, timeout=30, tmpdir=None, address_space=1 << 30):
    """Run one child with a bounded capture, scrubbed environment and OS limits."""
    home = _prepare_child_dir(tmpdir or cwd or tempfile.gettempdir())
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd,
                            env=child_env(home), preexec_fn=_child_preexec(address_space),
                            start_new_session=(os.name == "posix"))
    captured = {"stdout": bytearray(), "stderr": bytearray()}
    exceeded = set()
    lock = threading.Lock()

    def kill_child():
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except (OSError, ProcessLookupError):
            pass

    def collect(name, stream):
        while True:
            chunk = stream.read(65536)
            if not chunk:
                return
            with lock:
                remaining = _CHILD_OUTPUT_LIMIT - len(captured[name])
                if remaining > 0:
                    captured[name].extend(chunk[:remaining])
                if len(chunk) > remaining:
                    exceeded.add(name)
                    kill_child()

    readers = [threading.Thread(target=collect, args=(name, stream), daemon=True)
               for name, stream in (("stdout", proc.stdout), ("stderr", proc.stderr))]
    for reader in readers:
        reader.start()

    def write_stdin():
        try:
            proc.stdin.write(stdin.encode() if isinstance(stdin, str) else stdin)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    writer = None
    if stdin is not None:
        writer = threading.Thread(target=write_stdin, daemon=True)
        writer.start()
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_child()
        proc.wait()
        rc = 124
    if writer:
        writer.join(timeout=1)
    for reader in readers:
        reader.join(timeout=2)
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is not None:
            stream.close()
    out = captured["stdout"].decode(errors="replace")
    err = captured["stderr"].decode(errors="replace")
    if exceeded:
        detail = ", ".join(sorted(exceeded))
        return 125, out, f"Child {detail} exceeded the 1 MB output limit"
    if rc == 124:
        return 124, out, "TIMEOUT"
    return rc, out, err


STRICT = True
QUIET = False
_log_lock = threading.Lock()


def log(msg):
    if not QUIET:
        with _log_lock:
            print(f"    [{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def norm(s):
    s = s.replace("\r\n", "\n")
    if STRICT:
        return s[:-1] if s.endswith("\n") else s
    return "\n".join(ln.rstrip() for ln in s.split("\n")).strip()


def extract_java(text):
    m = re.search(r"```java\s*\n(.*?)```", text, re.S)
    if m:
        return m.group(1).strip()
    m = re.search(r"```\s*\n(.*?)```", text, re.S)
    if m and "class" in m.group(1):
        return m.group(1).strip()
    if "public class Main" in text:
        return text[text.index("public class Main"):].strip()
    return None


def compile_cobol(src, exe):
    _prepare_child_dir(Path(exe).parent)
    for fmt in ("-free", "-fixed"):
        rc, _, err = run([COBC, "-x", fmt, "-o", str(exe), str(src)], timeout=60,
                         tmpdir=Path(exe).parent)
        if rc == 0 and Path(exe).exists():
            return fmt, None
    return None, "COBOL compile failed: " + err.strip()[:300]


def ground_truth(cob, tests, wd):
    exe = wd / "cobol_prog"
    fmt, err = compile_cobol(cob, exe)
    if err:
        return None, None, err
    outs = []
    for t in tests:
        rc, out, e = run([str(exe)], stdin=t, timeout=20, tmpdir=wd)
        if rc != 0:
            return None, fmt, f"COBOL run failed (rc={rc}): {e.strip()[:200]}"
        outs.append(norm(out))
    return outs, fmt, None


def show(s, n=400):
    return repr(s)[:n]


def run_java_tests(java_src, wd, tests, expected):
    """Compile + run the Java on every test. Returns per-test detail for the UI."""
    if SAFE and BANNED_JAVA.search(java_src):
        return {"compile_error": "blocked: generated Java uses file, network, process or "
                                 "reflection APIs, which the hosted demo does not allow",
                "tests": []}
    _prepare_child_dir(wd)
    (wd / "Main.java").write_text(java_src)
    rc, out, err = run(["javac", "-J-Xmx256m", "-J-XX:+UseSerialGC",
                        "-J-XX:CompressedClassSpaceSize=128m",
                        "-J-XX:ActiveProcessorCount=2", "Main.java"],
                       cwd=wd, timeout=60, tmpdir=wd, address_space=2 << 30)
    if rc != 0:
        compile_error = err.strip() or out.strip() or f"javac exited with status {rc}"
        return {"compile_error": compile_error[:1200], "tests": []}
    out = []
    for i, (t, exp) in enumerate(zip(tests, expected), 1):
        rc, o, e = run(["java", "-Xmx256m", "-XX:+UseSerialGC",
                       "-XX:CompressedClassSpaceSize=128m", "-XX:ActiveProcessorCount=2",
                       "-cp", str(wd), "Main"], stdin=t,
                       timeout=20, cwd=wd, tmpdir=wd, address_space=2 << 30)
        act = norm(o)
        out.append({"i": i, "ok": rc == 0 and act == exp, "stdin": t[:300],
                    "expected": exp, "actual": act,
                    "diff_details": exact_line_diff(exp, act) if rc == 0 and act != exp else [],
                    "error": e.strip()[:800] if rc != 0 else None})
    return {"compile_error": None, "tests": out}


def line_diff(exp, act, max_show=16):
    """Difference for the model, aligned like diff: one extra or missing line is reported as that,
    not as every following line being different."""
    import difflib
    e, a = exp.split("\n"), act.split("\n")
    ops = difflib.SequenceMatcher(None, e, a, autojunk=False).get_opcodes()
    matched = sum(i2 - i1 for tag, i1, i2, j1, j2 in ops if tag == "equal")
    items = []
    for tag, i1, i2, j1, j2 in ops:
        if tag == "insert":
            items.append(f"Java printed {j2 - j1} EXTRA line(s) at Java line {j1 + 1}, before COBOL line "
                         f"{i1 + 1}, which COBOL did not print: {show(a[j1], 220)}")
        elif tag == "delete":
            items.append(f"Java did NOT print {i2 - i1} line(s) that COBOL printed at COBOL line {i1 + 1}: "
                         f"{show(e[i1], 220)}")
        elif tag == "replace":
            n = min(i2 - i1, j2 - j1)
            for k in range(n):
                items.append(f"line {i1 + k + 1}:\n  EXPECTED (COBOL): {show(e[i1 + k], 220)}"
                             f"\n  ACTUAL (Java):   {show(a[j1 + k], 220)}")
            if i2 - i1 > n:
                items.append(f"Java did NOT print {i2 - i1 - n} line(s) from COBOL line {i1 + n + 1}: "
                             f"{show(e[i1 + n], 220)}")
            elif j2 - j1 > n:
                items.append(f"Java printed {j2 - j1 - n} EXTRA line(s) at Java line {j1 + n + 1}: "
                             f"{show(a[j1 + n], 220)}")
    out = [f"{matched} of {len(e)} COBOL output lines appear in the Java output in the same order "
           f"(COBOL printed {len(e)} lines, Java printed {len(a)})."]
    out += items[:max_show]
    if len(items) > max_show:
        out.append(f"... and {len(items) - max_show} more differing lines.")
    return "\n".join(out)


def exact_line_diff(expected, actual):
    """Details for differing output lines, including visible whitespace."""
    expected_lines, actual_lines = expected.split("\n"), actual.split("\n")
    details = []
    for i in range(max(len(expected_lines), len(actual_lines))):
        left = expected_lines[i] if i < len(expected_lines) else ""
        right = actual_lines[i] if i < len(actual_lines) else ""
        if i < len(expected_lines) and i < len(actual_lines) and left == right:
            continue

        def exact(text):
            visible = text.replace("\t", "⇥").replace(" ", "·")
            if text.endswith((" ", "\t")):
                visible += " ⟵ trailing whitespace"
            return f"exact: {visible} ({len(text)} chars)"

        detail = {"line": i, "expected_exact": exact(left), "actual_exact": exact(right)}
        if re.sub(r"[ \t]", "", left) == re.sub(r"[ \t]", "", right) and left != right:
            detail["note"] = (f"whitespace differs: COBOL {len(left)} chars, "
                              f"Java {len(right)} chars")
        details.append(detail)
    return details


def feedback(d):
    """(ok, message-for-the-model) from run_java_tests output."""
    compile_error = d.get("compile_error")
    if compile_error:
        return False, "javac error:\n" + compile_error
    tests = d.get("tests")
    if not isinstance(tests, list) or not tests:
        return False, "No inputs were compared; cannot verify this Java program."
    for r in tests:
        if r["error"]:
            return False, f"Java runtime error on test {r['i']}:\n{r['error']}"
        if not r["ok"]:
            return False, (f"Output differs on test {r['i']}.\nSTDIN: {show(r['stdin'], 300)}\n"
                           + line_diff(r["expected"], r["actual"]))
    return True, None


def test_java(java_src, wd, tests, expected):
    return feedback(run_java_tests(java_src, wd, tests, expected))


_ctx = threading.local()


def cob_name_ctx():
    return getattr(_ctx, "name", "?")


def cap_for(tier):
    # Cheap tiers get a small budget: if they cannot answer briefly, escalate.
    default = {"nano": 6000, "super": 12000}.get(tier, MAX_TOKENS)
    return int(os.environ.get(f"PARITY_MAX_TOKENS_{tier.upper()}", default))


def solve_tier(tier, cobol, tests, expected, wd, fix_rounds, last_tier=True, on_event=None,
               deadline=None):
    def ev(**kw):
        if on_event:
            on_event(dict(kw, tier=tier))

    model = model_for(tier)
    base = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": f"Translate this COBOL program to Java:\n\n{cobol}"},
    ]
    msgs = base
    rec = {"tier": tier, "model": model, "rounds": [], "tin": 0, "tout": 0,
           "secs": 0.0, "solved": False, "java": None}
    no_think_next = False  # set after a reply was cut off: the next try must not spend tokens on thinking
    for _ in range(fix_rounds + 1):
        if deadline and time.time() > deadline:
            rec["stopped"] = "time"
            ev(type="note", text="Time budget reached: stopping here")
            break
        rnd = len(rec["rounds"]) + 1
        log(f"{cob_name_ctx()} -> {tier}: calling model (round {rnd})")
        ev(type="model_call", round=rnd, model=model)
        think = THINK_MODE == "on" or (THINK_MODE == "auto" and last_tier and rnd >= 3)
        if no_think_next:
            think, no_think_next = False, False
        try:
            r = chat(model, msgs, cap_for(tier), think=think,
                     on_progress=lambda n, s, rnd=rnd: ev(type="progress", round=rnd, tokens=n, secs=int(s)))
        except Exception:
            prompt = "\n".join(str(m.get("content", "")) for m in msgs)
            ev(type="model_reply", round=rnd, tin=estimate_tokens(prompt), tout=1,
               usage_estimated=True, secs=0, finish="error")
            raise
        log(f"{cob_name_ctx()} <- {tier}: {r['tout']} out-tok in {r['secs']:.0f}s, finish={r['finish']}")
        ev(type="model_reply", round=rnd, tout=r["tout"], tin=r["tin"],
           usage_estimated=r.get("usage_estimated", False),
           secs=round(r["secs"], 1), finish=r["finish"])
        rec["tin"] += r["tin"]
        rec["tout"] += r["tout"]
        rec["secs"] += r["secs"]
        entry = {"finish": r["finish"], "tout": r["tout"]}
        if r["finish"] in ("length", "timeout"):
            entry["status"] = "truncated" if r["finish"] == "length" else "timeout"
            if not last_tier:
                rec["rounds"].append(entry)
                log(f"{cob_name_ctx()} {tier}: hit token cap -> escalating")
                ev(type="note", text="hit the token or time limit: escalating to the next model")
                return rec
            no_think_next = True
            if msgs is base:
                ev(type="note", text="hit the token or time limit: retrying with a shorter-reasoning prompt")
                msgs = base[:1] + [{"role": "user", "content": (
                    "Translate this COBOL program to Java. Think briefly, then output only "
                    f"the final Main.java in one ```java block.\n\n{cobol}")}]
            else:
                # keep the last Java and its difference list; only the thinking is dropped
                ev(type="note", text="hit the token or time limit: retrying the same fix without extended thinking")
        else:
            java = extract_java(r["text"])
            if java is None:
                entry["status"] = "no_code"
                ev(type="note", text="reply had no code block: asking again")
                msgs = base + [
                    {"role": "assistant", "content": r["text"][:1500]},
                    {"role": "user", "content": "Your reply had no ```java code block. "
                     "Reply with only the full Main.java in one ```java block."}]
            else:
                ev(type="java", round=rnd, code=java)
                d = run_java_tests(java, wd, tests, expected)
                ok, fb = feedback(d)
                ev(type="tests", round=rnd, ok=ok, compile_error=d["compile_error"],
                   tests=d["tests"])
                if ok:
                    entry["status"] = "pass"
                    log(f"{cob_name_ctx()} {tier}: PASS")
                    rec["solved"] = True
                    rec["java"] = java
                    rec["rounds"].append(entry)
                    return rec
                entry["status"] = "fail"
                log(f"{cob_name_ctx()} {tier}: mismatch -> {fb.splitlines()[0][:80]}")
                entry["detail"] = fb[:600]
                msgs = base + [
                    {"role": "assistant", "content": f"```java\n{java}\n```"},
                    {"role": "user", "content": "That did not match the COBOL program's "
                     f"behavior.\n\n{fb}\n\nReturn the full corrected Main.java in one "
                     "```java block."}]
        rec["rounds"].append(entry)
    return rec


def run_case(cob, tiers, fix_rounds, outdir):
    name = cob.stem
    _ctx.name = name
    result = {"name": name, "valid": False, "solved": False, "solved_by": None,
              "tiers": [], "tin": 0, "tout": 0, "cost_usd": 0.0, "secs": 0.0, "error": None}
    try:
        ins = sorted(cob.parent.glob(f"{name}.*.in"))
        tests = [p.read_text(errors="replace") for p in ins] or [""]
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            expected, fmt, err = ground_truth(cob, tests, wd)
            if err:
                result["error"] = err
                return result
            result["valid"] = True
            result["cobol_format"] = fmt
            result["n_tests"] = len(tests)
            cobol = cob.read_text(errors="replace")
            for ti, tier in enumerate(tiers):
                jd = wd / f"java_{tier}"
                jd.mkdir()
                _prepare_child_dir(jd)
                rec = solve_tier(tier, cobol, tests, expected, jd, fix_rounds,
                                 last_tier=(ti == len(tiers) - 1))
                rec["cost_usd"] = cost_usd(tier, rec["tin"], rec["tout"])
                result["tiers"].append(rec)
                result["tin"] += rec["tin"]
                result["tout"] += rec["tout"]
                result["cost_usd"] += rec["cost_usd"]
                result["secs"] += rec["secs"]
                if rec["solved"]:
                    result["solved"] = True
                    result["solved_by"] = tier
                    result["java"] = rec["java"]
                    break
    except Exception as e:  # noqa: BLE001
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        (outdir / "cases").mkdir(parents=True, exist_ok=True)
        (outdir / "cases" / f"{name}.json").write_text(json.dumps(result, indent=2))
    return result


def main():
    global STRICT
    ap = argparse.ArgumentParser(description="Parity v2")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rp = sub.add_parser("run")
    rp.add_argument("path")
    rp.add_argument("--tiers", default="nano,super,ultra")
    rp.add_argument("--fix-rounds", type=int, default=1)
    rp.add_argument("--jobs", type=int, default=3)
    rp.add_argument("--limit", type=int, default=0)
    rp.add_argument("--filter", default="")
    rp.add_argument("--quiet", action="store_true", help="no live progress lines")
    rp.add_argument("--lenient", action="store_true",
                    help="ignore trailing whitespace when comparing outputs")
    a = ap.parse_args()
    STRICT = not a.lenient
    global QUIET
    QUIET = a.quiet

    tiers = [t.strip() for t in a.tiers.split(",") if t.strip()]
    for t in tiers:
        if t not in DEFAULT_MODELS:
            sys.exit(f"unknown tier {t}; use nano, super, ultra")
    p = Path(a.path)
    cases = [p] if p.is_file() else sorted(p.glob("*.cob"))
    cases = [c for c in cases if a.filter in c.stem]
    if a.limit:
        cases = cases[:a.limit]
    if not cases:
        sys.exit("no .cob files found")
    client()  # fail fast on missing env

    results_root = Path(os.environ.get("PARITY_RESULTS_DIR", ROOT / "results"))
    outdir = results_root / datetime.now().strftime("%Y%m%d-%H%M%S")
    outdir.mkdir(parents=True, exist_ok=True)
    print(f"{len(cases)} programs | tiers {tiers} | fix rounds {a.fix_rounds} | "
          f"{'strict' if STRICT else 'lenient'} compare | results -> {outdir}", flush=True)

    t0 = time.time()
    results = []
    with ThreadPoolExecutor(a.jobs) as ex:
        futs = [ex.submit(run_case, c, tiers, a.fix_rounds, outdir) for c in cases]
        for f in futs:
            r = f.result()
            results.append(r)
            if r["error"]:
                status = f"SKIP/ERROR ({r['error'][:70]})"
            elif r["solved"]:
                status = f"PASS by {r['solved_by']}"
            else:
                status = "FAIL (all tiers)"
            print(f"  {r['name']:<28} {status:<28} {r['tout']:>6} out-tok  "
                  f"{r['secs']:.0f}s", flush=True)

    valid = [r for r in results if r["valid"]]
    solved = [r for r in valid if r["solved"]]
    by_tier = {t: sum(1 for r in solved if r["solved_by"] == t) for t in tiers}
    tok = {t: {"in": 0, "out": 0, "usd": 0.0} for t in tiers}
    for r in valid:
        for rec in r["tiers"]:
            tok[rec["tier"]]["in"] += rec["tin"]
            tok[rec["tier"]]["out"] += rec["tout"]
            tok[rec["tier"]]["usd"] += rec["cost_usd"]
    total_usd = sum(v["usd"] for v in tok.values())
    summary = {"programs": len(cases), "valid": len(valid), "solved": len(solved),
               "solved_by_tier": by_tier, "tokens_and_cost": tok,
               "total_cost_usd_listprice": round(total_usd, 4),
               "wall_secs": round(time.time() - t0), "tiers": tiers,
               "fix_rounds": a.fix_rounds, "strict": STRICT}
    (outdir / "summary.json").write_text(json.dumps(summary, indent=2))

    print("\n=== SUMMARY ===")
    print(f"valid programs: {len(valid)}/{len(cases)}   equivalent (all tests pass): "
          f"{len(solved)}/{len(valid)}")
    print("solved by tier:", by_tier)
    for t in tiers:
        v = tok[t]
        print(f"  {t:<6} tokens in/out {v['in']}/{v['out']}  list-price ${v['usd']:.4f}")
    print(f"total list-price cost: ${total_usd:.4f}   (nano input price unknown, counted 0)")
    print(f"wall time: {summary['wall_secs']}s   details: {outdir}")


if __name__ == "__main__":
    main()
