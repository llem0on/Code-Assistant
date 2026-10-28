#!/usr/bin/env python3
"""Eval the QA pipeline with headless Claude and compare it against Claude without the skill.

A case is a folder in eval/dataset/:
  case.json           golden: scope, expected requirements, forbidden leaks, planted bugs (+ probe)
  conversation.jsonl  the build conversation in Claude Code transcript format, with a /qa-start line
  app/                the code that conversation produced (correct version)
  bugs/*.patch        planted regressions, applied on a copy of app/ before the run

Per case, two arms against the same conversation and the same patched app (cwd = app copy):
  skill:    transcript -> qa.sh start -> /qa-end -> "setuju" -> /qa-run
  baseline: whole conversation pasted to plain Claude (QA skill/agent denied) -> "tolong test API"
Both arms' test suites are re-run by qa.sh against a freshly started app and scored the same way.

  python3 eval/run.py [eval/dataset/<case> ...]         all cases by default; costs real tokens
  python3 eval/run.py --arms skill ...                  only one arm (skill | baseline | skill,baseline)
  python3 eval/run.py --rescore eval/results/<run>      re-score a past run after editing golden; no tokens
  python3 eval/run.py --score <case> <case-dir>         score one case dir; no tokens
  python3 eval/run.py --check-dataset                   validate dataset, app build and bug patches; no tokens

Needs: claude CLI (logged in), python3 stdlib, go, patch, and bash + jq + curl for qa.sh.
"""
import argparse
import csv
import filecmp
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

E = Path(__file__).resolve().parent
REPO = E.parent
QA = REPO / "scripts" / "qa.sh"
INSTALLED = ["commands/qa-start.md", "commands/qa-end.md", "commands/qa-run.md",
             "agents/qa-goal-extractor.md", "skills/qa-requirements/SKILL.md", "scripts/qa.sh"]
ROW = re.compile(r"^\|\s*`?[GCDU]-\d+")

SKILL_TOOLS = ["Bash(bash ~/.claude/scripts/qa.sh:*)", "Bash(QA_ALLOW_MUTATING=1 bash ~/.claude/scripts/qa.sh:*)",
               "Agent", "Skill", "Read", "Glob", "Grep"]
BASE_TOOLS = ["Bash(curl:*)", "Bash(jq:*)", "Read", "Glob", "Grep"]
BASE_DENY = ["Skill", "Agent"]  # baseline = Claude without the QA commands/skill/agent
CONFIRM = ("Setuju dengan rangkumannya. Untuk pertanyaan yang ada, pakai asumsi paling wajar "
           "dan tandai inferred. Lanjut bagi jadi test queue.")
RUN = "Queue sudah aku cek, setuju. Semua test mutating boleh dijalankan, ini API lokal. Jalankan /qa-run."
QA_START = "<command-name>/qa-start</command-name>"
GOENV = {"GOFLAGS": "-buildvcs=false", "CGO_ENABLED": "0"}
BASELINE = ("Tolong test API sesuai percakapan di atas. Jalankan test-nya langsung. "
            "Di akhir jawaban, tulis semua test yang kamu jalankan dalam satu blok ```json berisi array "
            '[{"title": "...", "method": "GET", "path": "/...", "body": null, "expect_status": 200, "status": "pass|fail"}].')


def load(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def bug_ids(case):
    return [b["id"] for b in case.get("bugs", [])]


def with_port(text, port):
    return text.replace("127.0.0.1:18080", f"127.0.0.1:{port}").replace("localhost:18080", f"localhost:{port}")


def port_open(port):
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def load_case(path):
    """A case folder (case.json + conversation.jsonl + app/ + bugs/), or a bare json (test fixture on dummy_api.py)."""
    p = Path(path).resolve()
    c = load(p / "case.json" if p.is_dir() else p)
    return dict(c, _dir=p if p.is_dir() else None)


def all_cases():
    return sorted(p for p in (E / "dataset").iterdir() if (p / "case.json").exists())


def conversation(c):
    """Transcript lines split at the /qa-start command: (before, from /qa-start on)."""
    lines = (c["_dir"] / "conversation.jsonl").read_text().splitlines()
    i = next(i for i, line in enumerate(lines) if QA_START in line)
    return lines[:i], lines[i:]


def message_texts(lines):
    """(role, text) for plain user/assistant text, the same view qa.sh gives the extractor."""
    for line in lines:
        m = json.loads(line)
        content = m.get("message", {}).get("content")
        if isinstance(content, str):
            yield m["type"], content
        else:
            for b in content or []:
                if b.get("type") == "text":
                    yield m["type"], b["text"]


def patched_app(c, bugs, dest):
    """Copy app/ to dest and apply the planted bug patches."""
    shutil.copytree(c["_dir"] / "app", dest)
    patches = {b["id"]: c["_dir"] / b["patch"] for b in c.get("bugs", [])}
    for bug in bugs:
        subprocess.run(["patch", "-p1", "-s", "-d", str(dest), "-i", str(patches[bug])], check=True)
    return dest


@contextmanager
def app_server(c, port, bugs=()):
    """The case's app with these bugs patched in, built and started fresh on port."""
    if port_open(port):
        sys.exit(f"port {port} sudah dipakai, pakai --port")
    with tempfile.TemporaryDirectory() as tmp:
        if c["_dir"]:
            src = patched_app(c, bugs, Path(tmp) / "app")
            subprocess.run(["go", "build", "-o", f"{tmp}/server", "."], cwd=src, env=dict(os.environ, **GOENV),
                           check=True, capture_output=True)
            cmd = [f"{tmp}/server"]
        else:
            cmd = [sys.executable, str(E / "dummy_api.py"), str(port), *bugs]
        srv = subprocess.Popen(cmd, env=dict(os.environ, PORT=str(port)), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                if port_open(port):
                    break
                time.sleep(0.1)
            yield f"http://127.0.0.1:{port}"
        finally:
            srv.terminate()
            srv.wait()


# ---------- generation (costs tokens) ----------

def render(lines, port):
    """Conversation as plain text for the baseline prompt, tool calls included (they were in its context)."""
    out = []
    for line in lines:
        m = json.loads(line)
        content = m.get("message", {}).get("content")
        if isinstance(content, str):
            if QA_START not in content:
                out.append(f"USER: {content}")
            continue
        for b in content or []:
            if b.get("type") == "text" and not b["text"].startswith("QA-START:"):
                out.append(f"{m['type'].upper()}: {b['text']}")
            elif b.get("type") == "tool_use":
                inp = b["input"]
                body = inp.get("content") or inp.get("command") or json.dumps(inp, ensure_ascii=False)
                out.append(f"ASSISTANT [tool {b['name']} {inp.get('file_path', '')}]:\n{body}")
            elif b.get("type") == "tool_result":
                out.append(f"[tool result]: {str(b.get('content'))[:1500]}")
    return with_port("\n\n".join(out), port)


def ask(cwd, out, prompt, a, tools, deny=(), session=None, env=None):
    cmd = ["claude", "-p", prompt, "--output-format", "json", "--max-budget-usd", str(a.budget),
           "--permission-mode", "acceptEdits", "--allowedTools", *tools]
    if deny:
        cmd += ["--disallowedTools", *deny]
    if session:
        cmd += ["--resume", session]
    if a.model:
        cmd += ["--model", a.model]
    r = subprocess.run(cmd, cwd=cwd, env=dict(os.environ, **(env or {})), capture_output=True, text=True)
    out.write_text(r.stdout)
    return load(out, {})


def gen_skill(case, d, a):
    patched_app(case, bug_ids(case), d / "proj")  # cwd = the repo as it is, bugs included
    (d / "tr").mkdir()
    tr = d / "tr" / "s.jsonl"
    before, after = conversation(case)
    tr.write_text("".join(with_port(line, a.port) + "\n" for line in before))
    env = {"QA_ROOT": str(d / "proj"), "QA_TRANSCRIPT_DIR": str(d / "tr")}
    subprocess.run(["bash", str(QA), "start"], env=dict(os.environ, **env), check=True, capture_output=True)
    with tr.open("a") as f:
        f.writelines(with_port(line, a.port) + "\n" for line in after)

    env = {"QA_TRANSCRIPT_DIR": str(d / "tr")}
    queue = d / "proj" / ".qa" / "queue.json"
    print(f"  skill turn 1: /qa-end {case['scope']}", flush=True)
    t1 = ask(d / "proj", d / "turn1.json", f"/qa-end {case['scope']}", a, SKILL_TOOLS, env=env)
    if queue.exists():
        (d / "queue_before_confirm").touch()  # skill must wait for confirmation
    sid = t1.get("session_id")
    if not sid:
        return
    print("  skill turn 2: setuju", flush=True)
    ask(d / "proj", d / "turn2.json", CONFIRM, a, SKILL_TOOLS, session=sid, env=env)
    if not queue.exists():
        return
    shutil.copy(queue, queue.with_name("queue.generated.json"))
    print("  skill turn 3: /qa-run", flush=True)
    with app_server(case, a.port, bug_ids(case)):
        ask(d / "proj", d / "turn3.json", RUN, a, SKILL_TOOLS, session=sid, env=env)
    if queue.exists():
        shutil.copy(queue, queue.with_name("queue.live.json"))


def gen_baseline(case, b, a):
    b.mkdir(parents=True)
    patched_app(case, bug_ids(case), b / "proj")
    convo = render(sum(conversation(case), []), a.port)
    print("  baseline: claude tanpa skill", flush=True)
    with app_server(case, a.port, bug_ids(case)):
        ask(b / "proj", b / "turn1.json", f"Ini percakapan kita sejauh ini:\n\n{convo}\n\n{BASELINE}",
            a, BASE_TOOLS, deny=BASE_DENY)


def generate(case_path, out, a):
    case = load_case(case_path)
    d = out / case["name"]
    d.mkdir(parents=True)
    print(f"▶ {case['name']}: {case.get('about', '')}", flush=True)
    if "skill" in a.arms:
        gen_skill(case, d, a)
        save_transcripts(d, d / "proj")
    if "baseline" in a.arms:
        gen_baseline(case, d / "baseline", a)
        save_transcripts(d / "baseline", d / "baseline" / "proj")
    return d


# ---------- scoring (no tokens) ----------

def requirement_rows(md):
    rows = []
    if md.exists():
        for line in md.read_text().splitlines():
            if ROW.match(line):
                c = [x.strip().replace("`", "") for x in line.split("|")]
                rows.append({"id": c[1], "req": c[2], "type": c[4].lower() if len(c) > 4 else "", "sec": c[1][:1]})
    return rows


def trips(test, bug):
    """A test trips a planted bug when its request matches one of the bug's signatures ("when", one or a list),
    whatever requirement it traces to."""
    sigs = bug["when"] if isinstance(bug["when"], list) else [bug["when"]]
    return any(test.get("method") == w["method"] and re.search(w["path"], test.get("path") or "") is not None
               and ("expect_status" not in w or str(test.get("expect_status")) == str(w["expect_status"])) for w in sigs)


def scope_types(c):
    first = (c.get("scope") or ".").split()[0]
    return ["api", "logic", "ui"] if first == "." else first.replace("request", "api").split(",")


def test_text(t):
    return " ".join(str(t.get(k) or "") for k in ("title", "method", "path", "expect_status", "expect_jq")) \
        + " " + json.dumps(t.get("body"), ensure_ascii=False)


def shape(t):
    """Request + expected status, with ids ($ID or digits) in the path made generic."""
    return t.get("method"), re.sub(r"/(\d+|\$\w+)(?=[/?]|$)", "/{id}", t.get("path") or ""), str(t.get("expect_status"))


def golden_hit(e, tests):
    """The tests contain E's golden expected output: its steps in order, same request and expected status.
    The last step's "match" regex, if any, must hit "<body json> | <expect_jq>" of that test, to tell apart
    requests of the same shape (bad email vs blank name, both POST /users 400)."""
    steps, i = e["output"], 0
    for t in tests:
        s = steps[i]
        if shape(t) == shape(s) and (i < len(steps) - 1 or re.search(
                s.get("match", ""), f"{json.dumps(t.get('body'))} | {t.get('expect_jq') or ''}")):
            i += 1
            if i == len(steps):
                return True
    return False


def run_api_tests(q, proj, port, c, bugs=None):
    """Re-run a test list with qa.sh against the freshly started case app (all its bugs unless given).
    ui/logic are skipped: ui needs a browser, logic would execute LLM-written shell commands."""
    with app_server(c, port, bug_ids(c) if bugs is None else bugs) as base:
        qf = proj / ".qa" / "queue.json"
        qf.parent.mkdir(parents=True, exist_ok=True)
        q = dict(q, approved=True, base_url=base, tests=[dict(t, status="pending") for t in q.get("tests", [])])
        qf.write_text(json.dumps(q, indent=2, ensure_ascii=False))
        env = dict(os.environ, QA_ROOT=str(proj), QA_ALLOW_MUTATING="1")
        with (proj.parent / "run.log").open("w") as log:
            for t in q["tests"]:
                if (t.get("type") or "api") == "api":
                    subprocess.run(["bash", str(QA), "run", t["id"]], env=env, stdout=log, stderr=subprocess.STDOUT)
        return load(qf)


def resolved(q):
    """Ran tests with $NAME in path filled from values saved during the run (qa.sh "save")."""
    v = (q or {}).get("vars") or {}
    return [dict(t, path=re.sub(r"\$(\w+)", lambda m: str(v.get(m[1], m[0])), t.get("path") or ""))
            for t in (q or {}).get("tests", [])]


def tokens(turns):
    usage = [u for t in turns for u in (t.get("modelUsage") or {}).values()]
    by_model = {}
    for t in turns:
        for model, u in (t.get("modelUsage") or {}).items():
            by_model[model] = by_model.get(model, 0) + u.get("costUSD", 0)
    return {
        "in": sum(u.get("inputTokens", 0) + u.get("cacheCreationInputTokens", 0) for u in usage),
        "cache_read": sum(u.get("cacheReadInputTokens", 0) for u in usage),
        "out": sum(u.get("outputTokens", 0) for u in usage),
        "cost": sum(u.get("costUSD", 0) for u in usage),
        "by_model": by_model,
        "seconds": round(sum(t.get("duration_ms") or 0 for t in turns) / 1000),
    }


# ---------- token breakdown per part (no tokens) ----------

# relative price per token type; absolute cost comes from each turn's modelUsage.costUSD
WEIGHT = {"in": 1, "write_5m": 1.25, "write_1h": 2, "read": 0.1, "out": 5}
TURN_NAMES = {"skill": ["1 /qa-end", "2 setuju -> queue", "3 /qa-run"], "baseline": ["baseline"]}


def session_files(cwd, sid):
    """Main + subagent transcripts Claude Code wrote for a headless session run in cwd."""
    d = Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(Path(cwd).resolve()))
    return d / f"{sid}.jsonl", sorted((d / sid / "subagents").glob("*.jsonl"))


def save_transcripts(d, cwd):
    """Copy the session transcripts next to the turn files, so the breakdown survives ~/.claude cleanup."""
    sid = load(d / "turn1.json", {}).get("session_id")
    if not sid:
        return
    main, subs = session_files(cwd, sid)
    (d / "transcript").mkdir(exist_ok=True)
    for f, name in [(main, "main.jsonl")] + [(f, f"sub-{f.name}") for f in subs]:
        if f.exists():
            shutil.copy(f, d / "transcript" / name)


def part_label(b):
    t = b.get("type")
    if t != "tool_use":
        return {"text": "jawaban teks", "thinking": "thinking"}.get(t, t)
    i, n = b.get("input") or {}, b["name"]
    if n == "Bash":
        m = re.search(r"qa\.sh (\S+)", i.get("command", ""))
        tool = re.search(r"\b(curl|jq|go|cat|ls|grep|sed|python3?|bash)\b", i.get("command", ""))
        return f"Bash qa.sh {m[1]}" if m else f"Bash {tool[1] if tool else 'lain'}"
    if n == "Skill":
        return f"Skill {i.get('skill')}"
    if n == "Agent":
        return f"Agent {i.get('subagent_type')}"
    if "file_path" in i:
        return f"{n} {Path(i['file_path']).name}"
    return n


def api_calls(path, sub=""):
    """One row per API call (assistant lines of one call share message.id): usage + what it produced."""
    calls = {}
    for line in path.read_text().splitlines():
        m = json.loads(line)
        if m.get("type") != "assistant" or not m.get("message", {}).get("id"):
            continue
        msg = m["message"]
        c = calls.setdefault(msg["id"], {"ts": m["timestamp"], "model": msg.get("model"), "usage": msg.get("usage") or {},
                                         "parts": [], "sub": sub})
        c["parts"] += [part_label(b) for b in msg.get("content") or []]
    return list(calls.values())


def prompt_times(path):
    """Timestamps of the eval's follow-up prompts in the main transcript = turn boundaries."""
    out = []
    for line in path.read_text().splitlines():
        m = json.loads(line)
        content = m.get("message", {}).get("content") if m.get("type") == "user" else None
        text = content if isinstance(content, str) else " ".join(b.get("text", "") for b in content or [] if isinstance(b, dict))
        if text.startswith((CONFIRM[:40], RUN[:40])):
            out.append(m["timestamp"])
    return out


def weight(u):
    cc = u.get("cache_creation") or {}
    w5, w1 = cc.get("ephemeral_5m_input_tokens"), cc.get("ephemeral_1h_input_tokens")
    if w5 is None and w1 is None:
        w5, w1 = u.get("cache_creation_input_tokens", 0), 0
    return (WEIGHT["in"] * u.get("input_tokens", 0) + WEIGHT["write_5m"] * (w5 or 0) + WEIGHT["write_1h"] * (w1 or 0)
            + WEIGHT["read"] * u.get("cache_read_input_tokens", 0) + WEIGHT["out"] * u.get("output_tokens", 0))


def steps(d, cwd, arm):
    """Every API call of an arm, labelled by turn and part, with an estimated cost.
    A turn's real cost per model (modelUsage) is split over that turn's calls by token-price weight;
    model usage not in the transcript (internal calls) becomes its own 'internal' row so totals still match."""
    turns = [load(f, {}) for f in sorted(d.glob("turn*.json"))]
    sid = (turns[0] if turns else {}).get("session_id")
    saved = d / "transcript" / "main.jsonl"
    main, subs = (saved, sorted((d / "transcript").glob("sub-*.jsonl"))) if saved.exists() else session_files(cwd, sid or "-")
    if not main.exists():
        return []
    calls = api_calls(main) + [c for f in subs for c in api_calls(f, sub=f.stem)]
    bounds = prompt_times(main)
    names = TURN_NAMES[arm]
    rows = []
    for c in sorted(calls, key=lambda c: c["ts"]):
        ti = min(sum(c["ts"] >= b for b in bounds), len(names) - 1)
        u, parts = c["usage"], [x for x in c["parts"] if x != "thinking"]
        label = (" + ".join(dict.fromkeys(parts)) or "thinking") + (" (+thinking)" if "thinking" in c["parts"] and parts else "")
        part = ("subagent " + parts[0] if c["sub"] and parts else
                "subagent" if c["sub"] else parts[0] if parts else "thinking")
        rows.append({"arm": arm, "turn": names[ti], "model": c["model"], "label": label, "part": part,
                     "in": u.get("input_tokens", 0), "cache_write": u.get("cache_creation_input_tokens", 0),
                     "cache_read": u.get("cache_read_input_tokens", 0), "out": u.get("output_tokens", 0),
                     "_w": weight(u), "_ti": ti})
    for ti, t in enumerate(turns[:len(names)]):
        for model, mu in (t.get("modelUsage") or {}).items():
            mine = [r for r in rows if r["_ti"] == ti and r["model"] == model]
            total = sum(r["_w"] for r in mine)
            for r in mine:
                r["cost"] = mu.get("costUSD", 0) * r["_w"] / total if total else 0
                r["cost_out"] = r["cost"] * WEIGHT["out"] * r["out"] / r["_w"] if r["_w"] else 0
            if not mine:
                rows.append({"arm": arm, "turn": names[ti], "model": model, "label": f"internal {model}",
                             "part": "internal (tidak ada di transcript)", "in": mu.get("inputTokens", 0),
                             "cache_write": mu.get("cacheCreationInputTokens", 0),
                             "cache_read": mu.get("cacheReadInputTokens", 0), "out": mu.get("outputTokens", 0),
                             "cost": mu.get("costUSD", 0), "_ti": ti})
                r = rows[-1]
                w = r["in"] + WEIGHT["write_5m"] * r["cache_write"] + WEIGHT["read"] * r["cache_read"] + WEIGHT["out"] * r["out"]
                r["cost_out"] = r["cost"] * WEIGHT["out"] * r["out"] / w if w else 0
    for i, r in enumerate(rows, 1):
        r.setdefault("cost", 0)
        r.setdefault("cost_out", 0)
        r["n"] = i
        r.pop("_w", None)
        r.pop("_ti", None)
    answer = max((r["n"] for r in rows if r["part"] == "jawaban teks"), default=0)  # final answer to the user
    for r in rows:
        r["phase"] = phase(r, answer)
    return rows


PHASES = ["1 define goal", "2 rancang test", "3 testing", "4 report", "baca kode", "lain (internal)"]


def phase(r, answer):
    """Pipeline phase of one API call, from what it did."""
    part, arm = r["part"], r["arm"]
    if part.startswith("internal"):
        return "lain (internal)"
    if part.startswith(("Read ", "Glob", "Grep")) and not part.startswith("Read segment"):
        return "baca kode"
    if r["n"] == answer or part == "Bash qa.sh report":
        return "4 report"
    if arm == "skill":
        if part.startswith("subagent") or part in ("Bash qa.sh end", "Agent qa-goal-extractor", "Write requirements.md"):
            return "1 define goal"
        return "3 testing" if r["turn"].startswith("3") else "2 rancang test"
    # baseline has no explicit goal step: text/thinking before testing = planning, tool calls = testing
    return "2 rancang test" if part in ("jawaban teks", "thinking") else "3 testing"


def subject(t):
    """Endpoint a test hits, ids normalised: 'GET /users/{id}'."""
    path = str(t.get("path") or t.get("url") or t.get("cmd") or "?").split("?")[0]
    path = re.sub(r"/(\d+|\$\w+)(?=/|$)", "/{id}", path)
    return f"{str(t.get('method') or t.get('type') or '').upper()} {path}".strip()


def per_subject(rows, tests):
    """Cost of designing + running tests, split over endpoints by their number of tests (estimate)."""
    cost = sum(r["cost"] for r in rows if r["phase"] in ("2 rancang test", "3 testing"))
    counts = {}
    for t in tests:
        counts[subject(t)] = counts.get(subject(t), 0) + 1
    n = sum(counts.values()) or 1
    return [{"subject": k, "tests": v, "cost": cost * v / n} for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]


def turns_in(d):
    return [t for t in (load(f) for f in sorted(d.glob("turn*.json"))) if isinstance(t, dict)]


def suite(c, tests, ran, rerun=None):
    """Metrics comparable across arms, from the test list itself (not requirement IDs).
    ran = live results (what the user saw); rerun = the same list re-run on a fresh app, for reproducibility."""
    again = {t["id"]: t.get("status") for t in rerun or []}
    types = scope_types(c)
    inscope = [e for e in c["expect"] if not e.get("optional") and e.get("testable") in types]
    bugs = c.get("bugs", [])
    fails = [t for t in ran if t.get("status") == "fail"]
    golden = [e for e in inscope if e.get("output")]
    return {
        "tests": len(tests),
        "coverage": [sum(1 for e in inscope if any(all(re.search(p, test_text(t), re.I) for p in e["all"])
                                                       for t in tests)), len(inscope)],
        "expected": [sum(1 for e in golden if golden_hit(e, tests)), len(golden)],
        "expected_miss": [e["id"] for e in golden if not golden_hit(e, tests)],
        "bugs": [sum(1 for b in bugs if any(trips(t, b) for t in fails)), len(bugs)],
        "wrong_fails": [t["id"] for t in fails if not any(trips(t, b) for b in bugs)],
        "blocked": [t["id"] for t in ran if t.get("status") == "blocked"],
        "not_repro": [t["id"] for t in ran if t["id"] in again and t.get("status") != again[t["id"]]],
        "leaks": [f"{t['id']}~{f}" for f in c.get("forbid", []) + c.get("forbid_tests", []) for t in tests
                  if re.search(f, test_text(t), re.I)],
    }


def baseline_queue(turn):
    """Parse the last ```json test array in baseline's answer into qa.sh queue tests."""
    for block in reversed(re.findall(r"```json\s*(.*?)```", turn.get("result") or "", re.S)):
        try:
            items = json.loads(block)
        except ValueError:
            continue
        if not isinstance(items, list):
            continue
        tests = []
        for i, t in enumerate((x for x in items if isinstance(x, dict)), 1):
            body = t.get("body")
            if isinstance(body, str):
                try:
                    body = json.loads(body)
                except ValueError:
                    pass
            u = urlparse(str(t.get("path") or "/"))
            tests.append({"id": f"B-{i}", "requirement_id": "-", "title": t.get("title", ""),
                          "method": str(t.get("method") or "GET").upper(),
                          "path": (u.path or "/") + (f"?{u.query}" if u.query else ""), "body": body,
                          "expect_status": t.get("expect_status"), "claimed": t.get("status"), "status": "pending"})
        return {"approved": True, "base_url": None, "tests": tests}
    return None


def score_skill(c, d, port):
    p = d / "proj"
    rows = requirement_rows(p / "requirements.md")
    q = load(p / ".qa" / "queue.generated.json")
    types = (load(p / ".qa" / "scope.json") or {}).get("types", [])
    rerun = resolved(run_api_tests(q, p, port, c) if q else None)
    live = load(p / ".qa" / "queue.live.json")
    ran = resolved(live) if live else rerun  # what /qa-run showed the user; older runs only have the re-run

    ex = []
    for e in c["expect"]:
        m = [r for r in rows if (r["sec"] == "U") == (e.get("section") == "U")
             and all(re.search(pat, r["req"], re.I) for pat in e["all"])]
        ex.append(dict(e, ids=[r["id"] for r in m], got=[r["type"] for r in m]))
    must = [e for e in ex if not e.get("optional")]
    found = [e for e in ex if e["ids"]]
    inscope = [e for e in must if e.get("testable") in types]
    tests = (q or {}).get("tests", [])
    bugs = c.get("bugs", [])
    fails = [t for t in ran if t.get("status") == "fail"]
    wrong = [t for t in fails if not any(trips(t, b) for b in bugs)]
    caught = [b for b in bugs if any(trips(t, b) for t in fails)]
    row_ids = [r["id"] for r in rows]
    matched = {i for e in ex for i in e["ids"]}
    turns = turns_in(d)
    return {
        "recall": [sum(1 for e in must if e["ids"]), len(must)],
        "missed": [e["id"] for e in must if not e["ids"]],
        "type_ok": [sum(1 for e in found if e.get("type") is None or e["type"] in e["got"]), len(found)],
        "leaks": [f"{r['id']}~{f}" for f in c.get("forbid", []) for r in rows
                  if r["sec"] in ("G", "C") and re.search(f, r["req"], re.I)],
        "extra": [r["id"] for r in rows if r["sec"] in ("G", "C") and r["id"] not in matched],
        "gate": q is not None and not (d / "queue_before_confirm").exists() and q.get("approved") is False,
        "tests": len(tests),
        "untraced": [t["id"] for t in tests if t.get("requirement_id") not in row_ids],
        "off_scope": [t["id"] for t in tests if (t.get("type") or "api") not in types],
        "coverage": [sum(1 for e in inscope if any(t.get("requirement_id") in e["ids"] for t in tests)), len(inscope)],
        "bugs": [len(caught), len(bugs)],
        "missed_bugs": [b["id"] for b in bugs if b not in caught],
        "false_pos": [t["id"] for t in wrong if t.get("inferred") is not True],
        "inferred_fail": [t["id"] for t in wrong if t.get("inferred") is True],
        "blocked": [t["id"] for t in ran if t.get("status") == "blocked"],
        "tokens": tokens(turns),
        "errors": [t.get("result") or t.get("subtype") for t in turns if t.get("is_error") is True],
        "denials": sum(len(t.get("permission_denials") or []) for t in turns),
        "compare": dict(suite(c, tests, ran, rerun), tokens=tokens(turns)),
    }


def score_baseline(c, b, port):
    turns = turns_in(b)
    q = baseline_queue(turns[0]) if turns else None
    if q:
        (b / "proj" / ".qa").mkdir(parents=True, exist_ok=True)
        (b / "proj" / ".qa" / "queue.generated.json").write_text(json.dumps(q, indent=2, ensure_ascii=False))
    tests = (q or {}).get("tests", [])
    rerun = resolved(run_api_tests(q, b / "proj", port, c) if q else None)
    # live = what baseline reported to the user
    live = [dict(t, status=t["claimed"] if t.get("claimed") in ("pass", "fail") else "unknown") for t in tests]
    return dict(suite(c, tests, live, rerun), tokens=tokens(turns), parsed=q is not None,
                denials=sum(len(t.get("permission_denials") or []) for t in turns))


def score(case_path, d, port):
    c, d = load_case(case_path), Path(d)
    s = {"case": c["name"]}
    if (d / "proj").exists():
        s.update(score_skill(c, d, port))
        s["steps"] = steps(d, d / "proj", "skill")
        s["subjects"] = per_subject(s["steps"], (load(d / "proj" / ".qa" / "queue.generated.json") or {}).get("tests", []))
    if (d / "baseline").exists():
        s["baseline"] = score_baseline(c, d / "baseline", port)
        s["baseline"]["steps"] = steps(d / "baseline", d / "baseline" / "proj", "baseline")
        s["baseline"]["subjects"] = per_subject(
            s["baseline"]["steps"], (load(d / "baseline" / "proj" / ".qa" / "queue.generated.json") or {}).get("tests", []))
    (d / "score.json").write_text(json.dumps(s, indent=2, ensure_ascii=False))
    print(json.dumps({"case": s["case"], "skill_bugs": s.get("bugs"), "baseline_bugs": s.get("baseline", {}).get("bugs")}),
          flush=True)
    return s


def all_steps(scores):
    return [dict(r, case=s["case"]) for s in scores for r in s.get("steps", []) + s.get("baseline", {}).get("steps", [])]


def token_breakdown(scores, out):
    """Summary section: tokens and cost per turn, per part, and the most expensive calls. Also writes steps.csv."""
    rows = all_steps(scores)
    if not rows:
        return []
    keys = ["case", "arm", "turn", "n", "phase", "model", "part", "label", "in", "cache_write", "cache_read", "out", "cost", "cost_out"]
    with (out / "steps.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    def agg(key):
        groups = {}
        for r in rows:
            g = groups.setdefault(key(r), {"calls": 0, "in": 0, "cache_write": 0, "cache_read": 0, "out": 0,
                                           "cost": 0.0, "cost_out": 0.0})
            g["calls"] += 1
            for k in ("in", "cache_write", "cache_read", "out", "cost", "cost_out"):
                g[k] += r[k]
        return groups

    arm_cost = {a: sum(r["cost"] for r in rows if r["arm"] == a) or 1 for a in {r["arm"] for r in rows}}
    head = "| Calls | Input | Cache write | Cache read | Output | Cost | Cost context | Cost output | % arm |"
    sep = "|---|---|---|---|---|---|---|---|---|"

    def line(name, g, arm):
        return (f"| {name} | {g['calls']} | {g['in']} | {g['cache_write']} | {g['cache_read']} | {g['out']} "
                f"| ${g['cost']:.2f} | ${g['cost'] - g['cost_out']:.2f} | ${g['cost_out']:.2f} | {100 * g['cost'] / arm_cost[arm]:.0f}% |")

    lines = ["", "## Token per bagian", "",
             "Cost per panggilan = estimasi: cost asli tiap turn per model dibagi ke panggilannya sesuai bobot harga "
             "(input 1, cache write 1.25/2, cache read 0.1, output 5). Cost context = membaca system prompt, percakapan, "
             "hasil tool (naik tiap panggilan karena context makin panjang); cost output = yang ditulis model, termasuk "
             "thinking. Panggilan pertama tiap sesi memuat system prompt + CLAUDE.md + daftar skill (cache write besar). "
             "Semua baris ada di `steps.csv`.", "",
             "### Per turn", "", "| Arm / turn " + head, "|---" + sep]
    for (arm, turn), g in sorted(agg(lambda r: (r["arm"], r["turn"])).items()):
        lines.append(line(f"{arm} · {turn}", g, arm))
    lines += ["", "### Per fase", "", "| Arm / fase " + head, "|---" + sep]
    for (arm, ph), g in sorted(agg(lambda r: (r["arm"], r["phase"])).items(), key=lambda kv: (kv[0][0], PHASES.index(kv[0][1]))):
        lines.append(line(f"{arm} · {ph}", g, arm))
    subjects = {}
    for sc in scores:
        for arm, subs in (("skill", sc.get("subjects", [])), ("baseline", sc.get("baseline", {}).get("subjects", []))):
            for x in subs:
                g = subjects.setdefault((arm, x["subject"]), [0, 0.0])
                g[0] += x["tests"]
                g[1] += x["cost"]
    if subjects:
        lines += ["", "### Per subject (endpoint)", "",
                  "Biaya fase rancang test + testing dibagi ke endpoint sesuai jumlah test-nya (estimasi: `run-all` "
                  "menjalankan semua test dalam satu panggilan, jadi token per test tidak tercatat terpisah).", "",
                  "| Arm / subject | Tests | Cost | % arm |", "|---|---|---|---|"]
        for (arm, sub), (n, cost) in sorted(subjects.items(), key=lambda kv: (kv[0][0], -kv[1][1])):
            lines.append(f"| {arm} · {sub} | {n} | ${cost:.2f} | {100 * cost / arm_cost[arm]:.0f}% |")
    lines += ["", "### Per bagian (jenis panggilan)", "", "| Arm / bagian " + head, "|---" + sep]
    for (arm, part), g in sorted(agg(lambda r: (r["arm"], r["part"])).items(), key=lambda kv: (kv[0][0], -kv[1]["cost"])):
        lines.append(line(f"{arm} · {part}", g, arm))
    lines += ["", "### 8 panggilan termahal", "", "| Case | Arm / turn | # | Label | Output | Cache read | Cost |",
              "|---|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: -r["cost"])[:8]:
        lines.append(f"| {r['case']} | {r['arm']} · {r['turn']} | {r['n']} | {r['label']} | {r['out']} "
                     f"| {r['cache_read']} | ${r['cost']:.2f} |")
    return lines


def summary(scores, out):
    def f(p):
        return f"{p[0]}/{p[1]}"

    def usd(x):
        return f"${x:.2f}"

    lines = ["# QA eval"]
    skill = [s for s in scores if "recall" in s]
    if skill:
        lines += ["", "## Skill: kualitas ekstraksi + queue", "",
                  "| Case | Recall | Type | Leak | Extra | Gate | Coverage | Untraced | Off-scope | Bugs caught "
                  "| False pos | Inferred fail | Blocked | Tokens in / cache read / out | Cost |",
                  "|" + "---|" * 15]
        for s in skill:
            t = s["tokens"]
            lines.append(f"| {s['case']} | {f(s['recall'])} | {f(s['type_ok'])} | {len(s['leaks'])} | {len(s['extra'])} "
                         f"| {'ok' if s['gate'] else 'FAIL'} | {f(s['coverage'])} | {len(s['untraced'])} "
                         f"| {len(s['off_scope'])} | {f(s['bugs'])} | {len(s['false_pos'])} | {len(s['inferred_fail'])} "
                         f"| {len(s['blocked'])} | {t['in']} / {t['cache_read']} / {t['out']} | {usd(t['cost'])} |")

    arms = [(s["case"], name, m) for s in scores
            for name, m in (("skill", s.get("compare")), ("baseline", s.get("baseline"))) if m]
    if arms:
        lines += ["", "## Skill vs Claude default", "",
                  "Bug dan wrong fail dinilai dari hasil live, yaitu yang dilihat user (hasil /qa-run untuk skill, status yang "
                  "dilaporkan untuk baseline). Tidak reproducible = test yang hasilnya berbeda saat daftar test-nya dijalankan "
                  "ulang oleh qa.sh ke app yang di-build ulang.", "",
                  "| Case | Arm | Tests | Coverage | Expected = golden | Bugs caught | Wrong fails | Leak | Tidak reproducible "
                  "| Tokens out | Cost | Waktu |",
                  "|" + "---|" * 12]
        for case, name, m in arms:
            t = m["tokens"]
            claim = len(m["not_repro"]) if "not_repro" in m else "-"
            note = "" if m.get("parsed", True) else " (JSON test tidak ada)"
            exp = f(m["expected"]) if "expected" in m else "-"
            lines.append(f"| {case} | {name}{note} | {m['tests']} | {f(m['coverage'])} | {exp} | {f(m['bugs'])} "
                         f"| {len(m['wrong_fails'])} | {len(m['leaks'])} | {claim} | {t['out']} | {usd(t['cost'])} | {t['seconds']}s |")
        lines.append("")
        for name in ("skill", "baseline"):
            ms = [m for _, n, m in arms if n == name]
            if ms:
                caught = [sum(m["bugs"][i] for m in ms) for i in (0, 1)]
                lines.append(f"- **{name}**: bugs {f(caught)} · wrong fails {sum(len(m['wrong_fails']) for m in ms)} "
                             f"· cost {usd(sum(m['tokens']['cost'] for m in ms))} · out {sum(m['tokens']['out'] for m in ms)} "
                             f"· waktu {sum(m['tokens']['seconds'] for m in ms)}s")

    for s in scores:
        lines += ["", f"### {s['case']}"]
        if "recall" in s:
            lines += [f"- skill missed requirement: {s['missed']} · missed bug: {s['missed_bugs']}",
                      f"- skill leaks: {s['leaks']} · extra (cek manual, bisa halusinasi): {s['extra']}",
                      f"- skill false pos: {s['false_pos']} · inferred fail: {s['inferred_fail']} · blocked: {s['blocked']}",
                      f"- skill errors: {s['errors']} · permission denials: {s['denials']}",
                      f"- skill expected beda/tidak ada vs golden: {s['compare'].get('expected_miss')}"]
        if "baseline" in s:
            b = s["baseline"]
            lines += [f"- baseline expected beda/tidak ada vs golden: {b.get('expected_miss')}",
                      f"- baseline wrong fails: {b['wrong_fails']} · leaks: {b['leaks']} · tidak reproducible: {b['not_repro']} "
                      f"· permission denials: {b['denials']}"]
    lines += token_breakdown(scores, out)
    text = "\n".join(lines) + "\n"
    (out / "summary.md").write_text(text)
    return text


def stale_install():
    """Headless claude runs the copy installed in ~/.claude; it must be this repo's version."""
    home = Path.home() / ".claude"
    return [f for f in INSTALLED if not (home / f).exists() or not filecmp.cmp(REPO / f, home / f, shallow=False)]


def run_cases(cases, a):
    """Generate + score each case into a new results dir. Costs tokens."""
    stale = stale_install() if "skill" in a.arms else []
    if stale:
        sys.exit(f"~/.claude beda dengan repo ({', '.join(stale)}). Jalankan: bash install.sh")
    out = E / "results" / time.strftime("%Y%m%d-%H%M%S")
    scores = []
    for cp in cases:
        d = generate(Path(cp), out, a)
        print("  score: ", end="", flush=True)
        scores.append(score(cp, d, a.port))
    return out, scores


def rescore(out, port):
    out = Path(out).resolve()
    dirs = [d for d in sorted(out.iterdir()) if d.is_dir()]
    gone = [d.name for d in dirs if not (E / "dataset" / d.name / "case.json").exists()]
    if gone:
        print(f"skip (case sudah tidak ada di dataset/): {gone}")
    return [score(E / "dataset" / d.name, d, port) for d in dirs if d.name not in gone]


def check_dataset(port):
    """Dataset validity, no tokens: the conversation has a /qa-start line and contains every golden
    pattern after it, the app builds with all bugs applied, every golden expected output ("output")
    passes on the clean app, and each bug's probe passes on the clean
    app but fails with that one patch applied."""
    problems = []
    for cd in all_cases():
        c = load_case(cd)
        try:
            seg = " ".join(text for _, text in message_texts(conversation(c)[1]))
        except StopIteration:
            problems.append(f"{c['name']}: conversation.jsonl tidak punya baris /qa-start")
            continue
        for e in c["expect"]:
            problems += [f"{c['name']} {e['id']}: '{p}' tidak ada di percakapan setelah /qa-start"
                         for p in e["all"] if not re.search(p, seg, re.I)]
        try:
            with app_server(c, port):
                pass
        except subprocess.CalledProcessError as err:
            problems.append(f"{c['name']}: app + semua patch gagal build/apply: {err.stderr or err}")
            continue
        for e in c["expect"]:
            if e.get("output"):
                with tempfile.TemporaryDirectory() as tmp:
                    steps = [dict(t, id=f"O-{i}") for i, t in enumerate(e["output"], 1)]
                    got = [t["status"] for t in run_api_tests({"tests": steps}, Path(tmp) / "proj", port, c, [])["tests"]]
                    if got != ["pass"] * len(steps):
                        problems.append(f"{c['name']} {e['id']}: expected output golden {got} di app tanpa bug, harusnya semua pass")
        for b in c.get("bugs", []):
            steps = [dict(t, id=f"P-{i}") for i, t in enumerate(b["probe"], 1)]
            if not trips(steps[-1], b):
                problems.append(f"{c['name']} {b['id']}: probe terakhir tidak cocok dengan 'when'")
            with tempfile.TemporaryDirectory() as tmp:
                for bugs, want in (([], ["pass"] * len(steps)), ([b["id"]], ["pass"] * (len(steps) - 1) + ["fail"])):
                    proj = Path(tmp) / ("on" if bugs else "off") / "proj"
                    got = [t["status"] for t in run_api_tests({"tests": steps}, proj, port, c, bugs)["tests"]]
                    if got != want:
                        problems.append(f"{c['name']} {b['id']} bug {'on' if bugs else 'off'}: probe {got}, harusnya {want}")
    return problems


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cases", nargs="*", help="dataset case folders (default: all in eval/dataset)")
    ap.add_argument("--arms", default="skill,baseline", help="skill | baseline | skill,baseline")
    ap.add_argument("--rescore", metavar="RUN_DIR")
    ap.add_argument("--score", nargs=2, metavar=("CASE", "CASE_DIR"))
    ap.add_argument("--check-dataset", action="store_true", help="validate dataset, app build and bug patches; no tokens")
    ap.add_argument("--model", default=os.environ.get("EVAL_MODEL"), help="default: your Claude Code default")
    ap.add_argument("--budget", type=float, default=float(os.environ.get("EVAL_BUDGET", 2)), help="USD cap per claude call")
    ap.add_argument("--port", type=int, default=int(os.environ.get("EVAL_PORT", 18080)))
    a = ap.parse_args()
    a.arms = a.arms.split(",")

    if a.check_dataset:
        problems = check_dataset(a.port)
        print("\n".join(problems) or f"dataset OK: {len(all_cases())} case")
        sys.exit(1 if problems else 0)
    if a.score:
        score(a.score[0], a.score[1], a.port)
        return
    if a.rescore:
        out = Path(a.rescore).resolve()
        scores = rescore(out, a.port)
    else:
        out, scores = run_cases(a.cases or all_cases(), a)
    print(summary(scores, out) + f"detail: {out}")


if __name__ == "__main__":
    main()
