#!/usr/bin/env python3
"""Group discussion between Claude and other coding agents over a shared transcript.

    python council.py <transcript.md> "<message>"         (or pipe the message on stdin)
    python council.py <transcript.md> "@codex @grok <message>"
    python council.py <transcript.md> --agents codex,grok "<message>"
    python council.py --check                              (which agents are installed)

Appends <message> as Claude's turn, sends the transcript to the agents it
@mentions (or to every selected agent if it mentions none, or @all) in parallel
(read-only), appends their replies and prints them. An agent that @mentions
another agent in its reply gets that agent a follow-up turn in the same round
(up to --hops times). Lines that @mention Claude are collected at the end.

Calls are stateless: every turn each agent re-reads the transcript, so each one
sees everyone's previous turns. If the previous call died before any reply was
written, its unanswered round is reused instead of starting a duplicate.

The agents work in the git root of the current directory (override with --cwd),
so this script can live anywhere and be used from any repo.

Cross-platform (macOS/Linux/Windows). Prompts go over stdin or a temp file
rather than argv because Windows caps command lines (8191 chars through .cmd
shims).
"""

from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TIMEOUT = int(os.environ.get("COUNCIL_TIMEOUT", "420"))
# A failure faster than this is retried once, with RETRY_TIMEOUT, so the round
# still fits the ~8 minute Bash budget.
FAST_FAIL = 60
RETRY_TIMEOUT = 90
# Above this many characters, old agent replies are collapsed in the prompt
# (never in the file on disk).
PROMPT_BUDGET = int(os.environ.get("COUNCIL_PROMPT_CHARS", "60000"))
# How many follow-up turns agent-to-agent @mentions can trigger within a round.
HOPS = int(os.environ.get("COUNCIL_HOPS", "1"))
# Set in every agent's environment. An agent that finds the council instructions
# (CLAUDE.md, skills) and tries to run this script itself is refused.
GUARD = "COUNCIL_PARTICIPANT"

PROMPT = """You are {name}, one of several coding agents ({roster}) in a group
discussion about the repository at {cwd} (your current directory). Claude moderates.

Rules:
- Read-only: inspect code freely, never modify files. Do not run council.py
  or start another group discussion; you are a participant in this one.
- Back every factual claim about the code with file:line. If you could not
  verify something, say "unverified".
- Form your own verdict from the code first, then engage with the others by
  name: what you agree with, what you disagree with and why (with evidence).
  Do not defer to the moderator or the majority; do not repeat earlier points.
- Addressing works like a group chat. Write @Name ({mentionable}) only when
  you need that participant to answer a specific question or defend a
  specific claim: an @mentioned agent is asked to reply right after you, and
  @Claude reaches the moderator (and through Claude, the user). Otherwise
  refer to others by plain name, without the @.
- Keep the investigation focused: read what you need to verify the claims at
  hand, not the whole repository. Be concise: one short paragraph per point.
- The LAST line of your reply must be exactly one of
  "Status: AGREE", "Status: DISAGREE" or "Status: NEED-INFO", plus a short reason.
  AGREE means you checked every code claim you rely on and have no remaining
  objection to the plan or conclusion on the table.

Transcript so far:
{transcript}

(End of transcript.) {task} Reminder:
read-only, cite file:line, own verdict first, last line is the Status line.
"""
TASK = "Write only your ({name}'s) reply for round {round}."
FOLLOW_UP_TASK = (
    "{callers} @mentioned you in round {round}. Write only your ({name}'s) follow-up: "
    "answer what was addressed to you, and update your verdict if it changed."
)


class Failed(Exception):
    pass


# --- child process lifetime ------------------------------------------------

_live: set[subprocess.Popen] = set()
_live_lock = threading.Lock()


_job = None  # Windows kill-on-close Job Object that every agent is placed in
_k32 = None


def _create_job_windows() -> None:
    """Create a kill-on-close Job Object for the agents.

    When this interpreter dies for any reason (including the Bash tool killing
    it), its handle closes and Windows kills every process in the job, even the
    node processes behind .cmd shims. Children are spawned suspended and
    assigned explicitly (see run()), because this process may sit in a job that
    allows silent breakaway (e.g. Microsoft Store Python), where inherited
    membership does not happen. Fails soft: without a job, taskkill remains.
    """
    global _job, _k32
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in
                    ("Read", "Write", "Other", "ReadBytes", "WriteBytes", "OtherBytes")]

    class BASIC_LIMIT(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BASIC_LIMIT),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.SetInformationJobObject.restype = wintypes.BOOL
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.AssignProcessToJobObject.restype = wintypes.BOOL
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

    job = k32.CreateJobObjectW(None, None)  # unnamed, handle not inheritable
    if not job:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW")
    info = EXTENDED_LIMIT()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):  # ExtendedLimitInformation
        raise OSError(ctypes.get_last_error(), "SetInformationJobObject")
    _job, _k32 = job, k32  # never closed: it must live as long as this process


def _adopt_suspended(proc: subprocess.Popen) -> None:
    """Assign a CREATE_SUSPENDED child to the job, then let it run."""
    import ctypes

    assigned = _k32.AssignProcessToJobObject(_job, int(proc._handle))
    status = ctypes.WinDLL("ntdll").NtResumeProcess(ctypes.c_void_p(int(proc._handle)))
    if status != 0:
        _kill_tree(proc)
        raise Failed(f"could not resume child process (NTSTATUS {status:#x})")
    if not assigned:
        print(f"[council] could not add pid {proc.pid} to the job; relying on taskkill", file=sys.stderr)


def _kill_tree(proc: subprocess.Popen) -> None:
    # A plain kill only reaches the direct child; on Windows that is usually a
    # .cmd shim, and the node process behind it would keep running.
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=15)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
        proc.wait(timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def _kill_all(*_args) -> None:
    with _live_lock:
        procs = list(_live)
    for p in procs:
        _kill_tree(p)


def _install_cleanup() -> None:
    atexit.register(_kill_all)
    if os.name == "nt":
        try:
            _create_job_windows()
        except Exception as exc:  # noqa: BLE001
            print(f"[council] job object unavailable ({exc}); relying on taskkill", file=sys.stderr)
        return
    # Children run in their own session (so killpg reaches their whole tree),
    # which also means they survive our death unless we kill them. SIGKILL
    # cannot be caught; SIGTERM/SIGHUP/SIGINT can.
    def handler(signum, _frame):
        _kill_all()
        sys.exit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, handler)


def run(cmd: list[str], cwd: Path, timeout: int, stdin: str | None = None) -> tuple[str, str, int]:
    # Per-agent extras, e.g. COUNCIL_GROK_ARGS="-m grok-4.7-build-fast".
    cmd = [*cmd, *shlex.split(os.environ.get(f"COUNCIL_{cmd[0].upper()}_ARGS", ""), posix=os.name != "nt")]
    exe = shutil.which(cmd[0])
    if exe is None:
        raise Failed(f"{cmd[0]} not found on PATH")
    proc = subprocess.Popen(
        [exe, *cmd[1:]],
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
        env={**os.environ, GUARD: "1"},
        start_new_session=os.name != "nt",
        creationflags=0x4 if _job else 0,  # CREATE_SUSPENDED until it is in the job
    )
    with _live_lock:
        _live.add(proc)
    try:
        if _job:
            _adopt_suspended(proc)
        stdout, stderr = proc.communicate(stdin, timeout=timeout)
        return stdout, stderr, proc.returncode
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        raise
    finally:
        with _live_lock:
            _live.discard(proc)


def tail(text: str) -> str:
    return text.strip()[-800:]


# --- agents ----------------------------------------------------------------
# Each returns (reply, exit_code) or raises Failed / TimeoutExpired.

def ask_codex(prompt: str, cwd: Path, timeout: int) -> tuple[str, int]:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "reply.md"
        # approval_policy=never: otherwise Codex can ask to escalate out of the
        # sandbox, and a config with approvals_reviewer=auto_review grants it.
        stdout, stderr, code = run(
            ["codex", "exec", "--sandbox", "read-only", "-c", 'approval_policy="never"',
             "--skip-git-repo-check", "--color", "never", "-o", str(out), "-"],
            cwd, timeout, prompt,
        )
        reply = out.read_text(encoding="utf-8", errors="replace").strip() if out.exists() else ""
    # codex prints the final message on stdout too; use it if -o came back empty.
    reply = reply or stdout.strip()
    if not reply:
        raise Failed(tail(stderr or stdout))
    return reply, code


def ask_opencode(prompt: str, cwd: Path, timeout: int) -> tuple[str, int]:
    # The plan agent denies edits but still allows bash. A stricter custom agent
    # or permission override makes OpenCode's free tier refuse the request, so
    # this relies on the model plus the post-round worktree check.
    stdout, stderr, code = run(["opencode", "run", "--agent", "plan", "--format", "json"], cwd, timeout, prompt)
    texts = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        part = event.get("part")
        if event.get("type") == "text" and isinstance(part, dict) and part.get("text"):
            texts.append(part)
    if not texts:
        raise Failed(tail(stderr or stdout))
    # Only the final message: earlier ones are progress chatter between tool calls.
    last = texts[-1].get("messageID")
    return "\n".join(t["text"] for t in texts if t.get("messageID") == last).strip(), code


def ask_grok(prompt: str, cwd: Path, timeout: int) -> tuple[str, int]:
    # Grok headless ignores stdin, so the prompt goes through a file.
    # --sandbox is not enforced on Windows and plan mode still lets the shell
    # write, so the tool list itself is restricted to reading. That also avoids
    # headless runs cancelling on a shell command that would need approval.
    with tempfile.TemporaryDirectory() as tmp:
        pf = Path(tmp) / "prompt.md"
        pf.write_text(prompt, encoding="utf-8")
        stdout, stderr, code = run(
            [
                "grok", "--prompt-file", str(pf), "--cwd", str(cwd),
                "--tools", "read_file,grep,list_dir,web_search,web_fetch",
                "--sandbox", "read-only", "--permission-mode", "plan",
                "--output-format", "streaming-messages-json", "--no-auto-update",
            ],
            cwd, timeout,
        )
    for line in reversed(stdout.splitlines()):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            if event.get("is_error") or not (event.get("result") or "").strip():
                # The event ends with a long usage block; the reason is in `errors`.
                raise Failed(tail(json.dumps(event.get("errors") or event)))
            # `result` is the final message only, without the progress chatter.
            return event["result"].strip(), code
    raise Failed(tail(stderr or stdout))


# key -> (display name, executable, function)
AGENTS = {
    "codex": ("Codex", "codex", ask_codex),
    "opencode": ("OpenCode", "opencode", ask_opencode),
    "grok": ("Grok", "grok", ask_grok),
}
LABELS = ["Claude", *(label for label, _, _ in AGENTS.values())]
HEADING = re.compile(rf"^### ({'|'.join(LABELS)}) \(round (\d+)(?:, follow-up \d+)?\)[ \t]*$", re.M)
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
# A code span: a backtick run, then anything up to the same-length run, within one paragraph.
INLINE_CODE = re.compile(r"(?<!`)(`+)(?!`)((?:(?!\n[ \t]*\n).)*?)(?<!`)\1(?!`)", re.S)
# "@codex", "@Grok," ... but not emails, paths or "x@grok".
MENTION = re.compile(rf"(?<![\w@./\\-])@({'|'.join(LABELS)}|all)\b", re.I)
STATUS = re.compile(r"^[\s*_`-]*status[*_`]*\s*[:\-]\s*[*_`]*\s*(AGREE|DISAGREE|NEED-INFO)\b", re.I)


def open_fence_at(text: str) -> tuple[list[int], str | None]:
    """Offsets of lines outside fenced code blocks, and the fence left open at the end.

    CommonMark rules: a fence closes only on the same character, at least as
    long, with nothing after it.
    """
    outside, fence, pos = [], None, 0
    for line in text.splitlines(keepends=True):
        m = FENCE.match(line.rstrip("\r\n"))
        if fence is None:
            outside.append(pos)
            if m:
                fence = m.group(1)
        elif m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not m.group(2).strip():
            fence = None
        pos += len(line)
    return outside, fence


def headings(text: str) -> list[re.Match]:
    """Council headings, ignoring look-alikes inside fenced code blocks."""
    outside = set(open_fence_at(text)[0])
    return [m for m in HEADING.finditer(text) if m.start() in outside]


def prose_lines(text: str) -> list[tuple[str, str]]:
    """(searchable, original) lines where @mentions count.

    Lines in code fences and blockquotes are dropped; code spans, which may
    cross lines within a paragraph, are blanked in the searchable copy.
    """
    outside = set(open_fence_at(text)[0])
    kept, pos = [], 0
    for line in text.splitlines(keepends=True):
        bare = line.rstrip("\r\n")
        prose = pos in outside and not FENCE.match(bare) and not bare.lstrip().startswith(">")
        kept.append(bare if prose else "")  # a blank line also ends any code span
        pos += len(line)
    # Spaces, not nothing, so the text around a span can't join into a new mention.
    searchable = INLINE_CODE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), "\n".join(kept)).split("\n")
    return [(s, o) for s, o in zip(searchable, kept) if o]


def mentions(text: str) -> list[str]:
    """Lower-cased mention targets ("codex", "claude", "all", ...) in order of first use."""
    found = [m.group(1).lower() for line, _ in prose_lines(text) for m in MENTION.finditer(line)]
    return list(dict.fromkeys(found))


def close_fences(reply: str) -> str:
    # An unclosed fence in one reply would hide every later heading from headings().
    fence = open_fence_at(reply)[1]
    return f"{reply}\n{fence}" if fence else reply


def ask(label: str, fn, prompt: str, cwd: Path) -> tuple[str, float]:
    start = time.monotonic()
    timeout = TIMEOUT
    for attempt in (1, 2):
        try:
            reply, code = fn(prompt, cwd, timeout)
            # Models sometimes echo their own heading; the script already writes it.
            reply = close_fences(re.sub(rf"^### {label}\b[^\n]*\n+", "", reply.lstrip()))
            if code:
                reply = f"(warning: {label} exited with code {code}; reply may be incomplete)\n\n{reply}"
            break
        except subprocess.TimeoutExpired:
            reply = f"(no reply: {label} timed out after {timeout}s)"
            break
        except Exception as exc:  # noqa: BLE001 — any failure becomes a visible transcript entry
            reply = f"(no reply: {label} failed)\n{exc}"
            if attempt == 1 and time.monotonic() - start < FAST_FAIL:
                print(f"[council] {label} failed fast, retrying once: {str(exc)[:200]}", file=sys.stderr, flush=True)
                timeout = RETRY_TIMEOUT
                continue
            break
    took = time.monotonic() - start
    print(f"[council] {label} finished in {took:.0f}s", file=sys.stderr, flush=True)
    return reply, took


def status_of(reply: str) -> str:
    if reply.startswith("(no reply"):
        return "NO-REPLY"
    lines = [l for l in reply.splitlines() if l.strip()]
    m = STATUS.match(lines[-1]) if lines else None
    return m.group(1).upper() if m else "?"


# --- transcript ------------------------------------------------------------

def sections(text: str) -> tuple[str, list[tuple[str, int, str]]]:
    """Split into (preamble, [(label, round, full section text)])."""
    heads = headings(text)
    if not heads:
        return text, []
    out = []
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        out.append((m.group(1), int(m.group(2)), text[m.start():end]))
    return text[: heads[0].start()], out


def prompt_view(text: str, round_no: int, transcript: Path) -> str:
    """What agents see: whole transcript, or with old agent replies collapsed if too long.

    Keeps the preamble, every Claude message (the brief and the moderation) and
    the previous round in full; older agent replies shrink to heading + status.
    """
    if len(text) <= PROMPT_BUDGET:
        return text
    pre, secs = sections(text)
    parts = [pre]
    for label, rnd, body in secs:
        if label == "Claude" or rnd >= round_no - 1:
            parts.append(body)
        else:
            head, _, reply = body.partition("\n")
            parts.append(f"{head}\n\n(collapsed to save space; full reply in {transcript.resolve()})\n"
                         f"Status: {status_of(reply.strip())}\n")
    return "".join(parts)


def git_root(start: Path) -> Path:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=start, capture_output=True,
            text=True, encoding="utf-8", errors="replace", check=True,
        ).stdout.strip()
        return Path(out) if out else start
    except (OSError, subprocess.CalledProcessError):
        return start


def worktree_fingerprint(cwd: Path, exclude: Path) -> str | None:
    """Hash of tracked changes + untracked file list, or None outside a git repo / on error.

    `exclude` (the transcript) is left out, since this script writes it mid-round.
    """
    spec = ["--", "."]
    try:
        spec.append(f":(exclude){exclude.resolve().relative_to(cwd).as_posix()}")
    except ValueError:
        pass  # transcript is outside the repo
    h = hashlib.sha256()
    for cmd in (["git", "status", "--porcelain", "--untracked-files=all", *spec],
                ["git", "diff", "HEAD", "--binary", *spec]):
        try:
            proc = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        h.update(proc.stdout)
    return h.hexdigest()


def main() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    if os.environ.get(GUARD):
        sys.exit("council.py: refusing to run inside a council participant")

    ap = argparse.ArgumentParser(description="Group discussion between Claude and other coding agents.")
    ap.add_argument("transcript", nargs="?", type=Path, help="transcript .md file (created if missing)")
    ap.add_argument("message", nargs="*", help="Claude's message for this round (default: stdin)")
    ap.add_argument(
        "--agents",
        default=os.environ.get("COUNCIL_AGENTS", ",".join(AGENTS)),
        help=f"comma-separated subset of {','.join(AGENTS)} (default: all installed; env COUNCIL_AGENTS)",
    )
    ap.add_argument("--cwd", type=Path, help="repository the agents inspect (default: git root of the current dir)")
    ap.add_argument("--hops", type=int, default=HOPS,
                    help="follow-up turns agent @mentions may trigger per round (default 1; env COUNCIL_HOPS; 0 = none)")
    ap.add_argument("--check", action="store_true", help="list which agents are installed and exit")
    args = ap.parse_intermixed_args()

    if args.check:
        for label, exe, _ in AGENTS.values():
            print(f"{label:9} {shutil.which(exe) or 'NOT FOUND'}")
        return
    if args.transcript is None:
        ap.error("transcript path is required")

    wanted = [a.strip().lower() for a in args.agents.split(",") if a.strip()]
    unknown = [a for a in wanted if a not in AGENTS]
    if unknown:
        ap.error(f"unknown agent(s): {', '.join(unknown)} (choose from {', '.join(AGENTS)})")
    installed = {key for key, (_, exe, _) in AGENTS.items() if shutil.which(exe)}

    msg = " ".join(args.message) or sys.stdin.read()
    if not msg.strip():
        sys.exit("empty message")

    # @mentions in the message pick who answers; none (or @all) means every selected agent.
    named = mentions(msg)
    targets = [m for m in named if m in AGENTS] if "all" not in named else []
    targets = targets or wanted
    missing = [AGENTS[key][0] for key in targets if key not in installed]
    if missing:
        print(f"[council] not installed, skipping: {', '.join(missing)}", file=sys.stderr)
    targets = [key for key in targets if key in installed]
    if not targets:
        sys.exit("council.py: none of the selected agents are installed")

    _install_cleanup()
    cwd = (args.cwd or git_root(Path.cwd())).resolve()
    transcript = args.transcript
    transcript.parent.mkdir(parents=True, exist_ok=True)
    text = transcript.read_text(encoding="utf-8-sig", errors="replace") if transcript.exists() else ""
    if not text:
        text = f"# Council transcript\n\nRepository: `{cwd}`\n"

    heads = headings(text)
    round_no = max((int(m.group(2)) for m in heads), default=0) + 1
    if heads and heads[-1].group(1) == "Claude":
        # The previous call died before writing any reply: reuse that round.
        round_no = int(heads[-1].group(2))
        text = text[: heads[-1].start()].rstrip("\n") + "\n"
        print(f"[council] round {round_no} had no replies; replacing it", file=sys.stderr)
    # Closed like agent replies: an open fence would hide every later heading,
    # and the next run would mistake this round for an unanswered one.
    text += f"\n### Claude (round {round_no})\n\n{close_fences(msg.strip())}\n"
    transcript.write_text(text, encoding="utf-8")

    # Agents can pull in each other only from the pool (--agents) plus whoever Claude named.
    allowed = [key for key in AGENTS if key in installed and (key in wanted or key in targets)]
    mentionable = ", ".join([*(f"@{AGENTS[key][0]}" for key in allowed), "@Claude"])
    participants = list(targets)
    print(f"[council] round {round_no}: asking {', '.join(AGENTS[k][0] for k in targets)} in {cwd} "
          f"(timeout {TIMEOUT}s, follow-ups {args.hops})", file=sys.stderr, flush=True)

    before = worktree_fingerprint(cwd, transcript)
    latest: dict[str, tuple[str, float]] = {}  # agent -> (last reply, total seconds this round)
    to_claude: list[tuple[str, str]] = []
    unanswered: dict[str, list[str]] = {}
    batch = {key: TASK.format(name=AGENTS[key][0], round=round_no) for key in targets}
    hop = 0
    while batch:
        roster = ", ".join(["Claude", *(AGENTS[k][0] for k in participants)])
        view = prompt_view(text, round_no, transcript)
        if view is not text and hop == 0:
            print(f"[council] transcript is {len(text)} chars; collapsed old replies to {len(view)} for the prompt",
                  file=sys.stderr)
        with ThreadPoolExecutor(len(batch)) as pool:
            futures = [
                (key, pool.submit(ask, AGENTS[key][0], AGENTS[key][2], PROMPT.format(
                    name=AGENTS[key][0], roster=roster, mentionable=mentionable, cwd=cwd,
                    transcript=view, task=task), cwd))
                for key, task in batch.items()
            ]
            results = [(key, *f.result()) for key, f in futures]

        suffix = f", follow-up {hop}" if hop else ""
        replies = "".join(f"\n### {AGENTS[key][0]} (round {round_no}{suffix})\n\n{reply}\n"
                          for key, reply, _ in results)
        text += replies
        with transcript.open("a", encoding="utf-8") as fh:
            fh.write(replies)
        print(replies, flush=True)

        # Who got @mentioned, and by whom, in this batch.
        callers: dict[str, list[str]] = {}
        for key, reply, took in results:
            latest[key] = (reply, latest.get(key, ("", 0.0))[1] + took)
            if reply.startswith("(no reply"):
                continue
            label = AGENTS[key][0]
            # Detect on the searchable copy, show the original so code spans survive.
            to_claude += [(label, original.strip()) for line, original in prose_lines(reply)
                          if any(m.group(1).lower() == "claude" for m in MENTION.finditer(line))]
            for m in mentions(reply):
                if m in AGENTS and m != key:
                    callers.setdefault(m, []).append(label)
        skipped = [AGENTS[k][0] for k in callers if k not in allowed]
        if skipped:
            print(f"[council] mentioned but not installed or not in --agents: {', '.join(skipped)}", file=sys.stderr)
        callers = {k: who for k, who in callers.items() if k in allowed}
        hop += 1
        if callers and hop > args.hops:
            unanswered = callers
            break
        batch = {key: FOLLOW_UP_TASK.format(callers=" and ".join(who), name=AGENTS[key][0], round=round_no)
                 for key, who in callers.items()}
        participants += [k for k in batch if k not in participants]
        if batch:
            print(f"[council] follow-up {hop}: asking "
                  + ", ".join(f"{AGENTS[k][0]} (mentioned by {' and '.join(w)})" for k, w in callers.items()),
                  file=sys.stderr, flush=True)

    # Checked after the replies are saved, so a slow git cannot lose them.
    after = worktree_fingerprint(cwd, transcript) if before is not None else None
    warning = None
    if before is not None and after is None:
        warning = f"\n> **NOTE (round {round_no}):** the post-round `git status` check failed; writes were not checked.\n"
    elif before is not None and before != after:
        warning = (
            f"\n> **WARNING (round {round_no}):** the working tree in `{cwd}` changed while the agents ran. "
            "An agent (or you, in parallel) may have written files; check `git status` / `git diff`.\n"
        )
    if warning:
        with transcript.open("a", encoding="utf-8") as fh:
            fh.write(warning)
        print(warning)

    if to_claude:
        print("--- addressed to @Claude:")
        for label, line in to_claude:
            print(f"  {label}: {line if len(line) <= 600 else line[:600] + ' …'}")
    if unanswered:
        print(f"--- not asked (follow-up limit {args.hops}): "
              + ", ".join(f"{AGENTS[k][0]} <- {' and '.join(w)}" for k, w in unanswered.items()))
    summary = " | ".join(f"{AGENTS[key][0]}: {status_of(reply)} ({took:.0f}s)" for key, (reply, took) in latest.items())
    print(f"--- round {round_no} status: {summary}\n--- transcript: {transcript.resolve()}")


if __name__ == "__main__":
    main()
