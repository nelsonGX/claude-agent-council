#!/usr/bin/env python3
"""Group discussion between Claude and other coding agents over a shared transcript.

    python3 council.py <transcript.md> "<message>"         (or pipe the message on stdin)
    python3 council.py <transcript.md> "@codex @grok <message>"
    python3 council.py <transcript.md> --agents codex,grok "<message>"
    python3 council.py --check [--json]                     (installed agents, profiles, budgets)
    python3 council.py --init [--force]                     (write a starter config)

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

Agent profiles (enabled, cost tier, turn budgets, strengths, extra args, custom
agents) come from a user config file and, for strengths and roles only, an
optional .agent-council.json in the repo; see config_paths() and load_config().

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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

TIMEOUT = int(os.environ.get("COUNCIL_TIMEOUT", "420"))
# A failure faster than this is retried once, with RETRY_TIMEOUT, so the round
# still fits the ~8 minute Bash budget.
FAST_FAIL = 60
RETRY_TIMEOUT = 90
RATE_LIMIT_WAIT = 20
# Failures a retry can't fix this round, matched in the agent's error output.
UNAVAILABLE = [
    ("quota", re.compile(r"(?:status|http_status)\W{0,3}402\b|payment required|quota|balance exhausted"
                         r"|insufficient (?:credit|balance|funds)|usage limit|out of credits", re.I)),
    ("auth", re.compile(r"(?:status|http_status)\W{0,3}401\b|unauthori[sz]ed|not logged in|log ?in required"
                        r"|please (?:log|sign) ?in|invalid api key", re.I)),
]
RATE_LIMITED = re.compile(r"(?:status|http_status)\W{0,3}429\b|rate.?limit|too many requests", re.I)
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
{role}
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
{profiles}
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


def run(cmd: list[str], cwd: Path, timeout: int, stdin: str | None, agent: Agent) -> tuple[str, str, int]:
    # Per-agent extras: "args" in the config, then e.g. COUNCIL_GROK_ARGS="-m grok-4.7-build-fast".
    env_name = "COUNCIL_" + re.sub(r"\W", "_", agent.key).upper() + "_ARGS"
    env_args = os.environ.get(env_name, "")
    cmd = [*cmd, *agent.args, *shlex.split(env_args, posix=os.name != "nt")]
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

def ask_codex(prompt: str, cwd: Path, timeout: int, agent: Agent) -> tuple[str, int]:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "reply.md"
        # approval_policy=never: otherwise Codex can ask to escalate out of the
        # sandbox, and a config with approvals_reviewer=auto_review grants it.
        stdout, stderr, code = run(
            ["codex", "exec", "--sandbox", "read-only", "-c", 'approval_policy="never"',
             "--skip-git-repo-check", "--color", "never", "-o", str(out), "-"],
            cwd, timeout, prompt, agent,
        )
        reply = out.read_text(encoding="utf-8", errors="replace").strip() if out.exists() else ""
    # codex prints the final message on stdout too; use it if -o came back empty.
    reply = reply or stdout.strip()
    if not reply:
        raise Failed(tail(stderr or stdout))
    return reply, code


def ask_opencode(prompt: str, cwd: Path, timeout: int, agent: Agent) -> tuple[str, int]:
    # The plan agent denies edits but still allows bash. A stricter custom agent
    # or permission override makes OpenCode's free tier refuse the request, so
    # this relies on the model plus the post-round worktree check.
    stdout, stderr, code = run(["opencode", "run", "--agent", "plan", "--format", "json"],
                               cwd, timeout, prompt, agent)
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


def ask_grok(prompt: str, cwd: Path, timeout: int, agent: Agent) -> tuple[str, int]:
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
            cwd, timeout, None, agent,
        )
    for line in reversed(stdout.splitlines()):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            # `result` is the final message only, without the progress chatter.
            result = (event.get("result") or "").strip()
            # The event ends with a long usage block; the reason is in `errors`.
            error = tail(json.dumps(event.get("errors") or event))
            if not result:
                raise Failed(error)
            if event.get("is_error"):
                # E.g. the quota ran out mid-turn: keep what it wrote, flagged.
                return f"(warning: Grok stopped with an error; reply may be incomplete: {error})\n\n{result}", code
            return result, code
    raise Failed(tail(stderr or stdout))


def ask_custom(prompt: str, cwd: Path, timeout: int, agent: Agent) -> tuple[str, int]:
    # A user-defined agent: the prompt goes in {prompt_file} if the command
    # names it, else over stdin; the reply is whatever it prints on stdout.
    # Read-only is up to that CLI's own flags plus the post-round worktree check.
    with tempfile.TemporaryDirectory() as tmp:
        pf = Path(tmp) / "prompt.md"
        via_file = any("{prompt_file}" in a for a in agent.command)
        if via_file:
            pf.write_text(prompt, encoding="utf-8")
        cmd = [a.replace("{prompt_file}", str(pf)).replace("{cwd}", str(cwd)) for a in agent.command]
        stdout, stderr, code = run(cmd, cwd, timeout, None if via_file else prompt, agent)
    if not stdout.strip():
        raise Failed(tail(stderr or stdout))
    return stdout.strip(), code


# --- agent profiles and config ---------------------------------------------

@dataclass
class Agent:
    key: str
    label: str
    exe: str
    fn: Callable
    command: list[str] = field(default_factory=list)  # custom agents only
    enabled: bool = True
    auto_join: bool = True  # joins rounds that @mention nobody; else only when Claude @mentions it
    cost: str = ""
    strengths: list[str] = field(default_factory=list)
    avoid: list[str] = field(default_factory=list)
    notes: str = ""
    instructions: str = ""  # private to this agent: a role or standing instruction
    args: list[str] = field(default_factory=list)
    timeout: int = TIMEOUT
    budget: dict[str, int] = field(default_factory=dict)  # BUDGET_KEYS -> max turns


BUILTIN = {
    "codex": ("Codex", ask_codex),
    "opencode": ("OpenCode", ask_opencode),
    "grok": ("Grok", ask_grok),
}
# What `--init` writes and the setup skill proposes. Opinions, not facts about
# the user's plan: cost and budget depend on the account.
SUGGESTED = {
    "codex": {
        "cost": "limited",
        "strengths": ["backend and APIs", "deep code audits", "concurrency and state machines", "tests"],
        "notes": "Thorough and precise but slow. Enforced read-only sandbox; can run read-only commands such as git log.",
    },
    "opencode": {
        "cost": "free",
        "strengths": ["quick second opinions", "frontend and UI code", "docs and developer experience"],
        "notes": "Quality depends on the configured model. Has a shell, so it can run read-only commands.",
    },
    "grok": {
        "cost": "free",
        "strengths": ["security review", "devil's advocate", "web lookups (docs, CVEs, changelogs)", "broad sweeps"],
        "notes": "No shell: reads, greps and searches the web, but can't run git or scripts. Usually the slowest.",
    },
}
COSTS = {
    "free": "free, @mention freely",
    "cheap": "cheap, @mention when useful",
    "limited": "limited, @mention only when its strengths are needed",
    "expensive": "expensive, @mention only for decisive questions",
}
# Turn budgets: per transcript, and over rolling windows tracked in usage.json.
BUDGET_KEYS = {"per_discussion": None, "per_day": 86400, "per_week": 7 * 86400}
FIELDS = {
    "enabled": bool, "auto_join": bool, "cost": str, "strengths": list, "avoid": list, "notes": str,
    "instructions": str, "args": list, "timeout": int, "budget": dict, "label": str, "command": list,
}
# A repo's .agent-council.json may only describe how agents fit that project.
# Commands, args, cost and budgets belong to the user's own accounts and machine:
# a cloned repo must not be able to run programs or loosen a sandbox.
PROJECT_FIELDS = {"enabled", "auto_join", "strengths", "avoid", "notes", "instructions"}
DEFAULTS = {"agents": list, "hops": int, "timeout": int, "prompt_chars": int}
PROJECT_DEFAULTS = {"agents", "hops"}
LABEL_OK = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,30}$")


def config_paths(cwd: Path) -> tuple[Path, Path]:
    """(user config, project config). COUNCIL_CONFIG overrides the user file."""
    if os.environ.get("COUNCIL_CONFIG"):
        user = Path(os.environ["COUNCIL_CONFIG"]).expanduser()
    else:
        user = state_dir() / "config.json"
    return user, cwd / ".agent-council.json"


def state_dir() -> Path:
    if os.name == "nt" and os.environ.get("APPDATA"):
        base = Path(os.environ["APPDATA"])
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "agent-council"


def _read_json(path: Path, warnings: list[str]) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        warnings.append(f"{path}: ignored, not valid JSON ({exc})")
        return {}
    if not isinstance(data, dict):
        warnings.append(f"{path}: ignored, top level must be an object")
        return {}
    return data


def _checked(entry: dict, allowed: dict | set, where: str, warnings: list[str]) -> dict:
    """The fields of entry that are allowed and of the right type."""
    out = {}
    for name, value in entry.items():
        if name not in allowed:
            if name in FIELDS or name in DEFAULTS:
                warnings.append(f"{where}: ignored {name!r}, which only the user config may set")
            else:
                warnings.append(f"{where}: ignored unknown field {name!r}")
            continue
        kind = (FIELDS | DEFAULTS)[name]
        if value is not None and not (isinstance(value, kind) and not (kind is int and isinstance(value, bool))):
            warnings.append(f"{where}: ignored {name!r}, expected {kind.__name__}")
            continue
        if kind is list and value is not None and not all(isinstance(v, str) for v in value):
            warnings.append(f"{where}: ignored {name!r}, expected a list of strings")
            continue
        out[name] = value
    return out


def load_config(cwd: Path) -> tuple[dict[str, Agent], dict, list[str], list[str]]:
    """(agents by key, defaults, config files loaded, warnings).

    Built-in agents always exist (enabled unless the config says otherwise);
    custom agents need a "command" in the user config.
    """
    warnings: list[str] = []
    user_path, project_path = config_paths(cwd)
    loaded = []
    layers = []
    for path, fields, defaults in ((user_path, FIELDS, DEFAULTS), (project_path, PROJECT_FIELDS, PROJECT_DEFAULTS)):
        data = _read_json(path, warnings)
        if data:
            loaded.append(str(path))
        for name in data:
            if name not in ("agents", "defaults", "$schema", "_comment"):
                warnings.append(f"{path}: ignored unknown top-level field {name!r}")
        agents = data.get("agents") or {}
        if not isinstance(agents, dict):
            warnings.append(f"{path}: ignored \"agents\", expected an object keyed by agent name")
            agents = {}
        checked = {}
        for key, entry in agents.items():
            if not isinstance(entry, dict):
                warnings.append(f"{path}: ignored agent {key!r}, expected an object")
                continue
            checked[key.lower()] = _checked(entry, fields, f"{path} agents.{key}", warnings)
        dflt = data.get("defaults") or {}
        layers.append((path, checked, _checked(dflt, defaults, f"{path} defaults", warnings)
                       if isinstance(dflt, dict) else {}))

    defaults: dict = {}
    for _, _, d in layers:
        defaults.update({k: v for k, v in d.items() if v is not None})
    base_timeout = int(os.environ["COUNCIL_TIMEOUT"]) if os.environ.get("COUNCIL_TIMEOUT") else \
        defaults.get("timeout", TIMEOUT)

    agents: dict[str, Agent] = {k: Agent(k, label, k, fn, timeout=base_timeout) for k, (label, fn) in BUILTIN.items()}
    for path, entries, _ in layers:
        is_user = path == user_path
        for key, entry in entries.items():
            if key not in agents:
                if not is_user:
                    warnings.append(f"{path}: ignored agent {key!r}; new agents can only be added in the user config")
                    continue
                label = entry.get("label") or key.capitalize()
                if not entry.get("command"):
                    warnings.append(f"{path}: ignored agent {key!r}: a custom agent needs \"command\"")
                    continue
                agents[key] = Agent(key, label, entry["command"][0], ask_custom, timeout=base_timeout)
            a = agents[key]
            for name, value in entry.items():
                if value is None:
                    continue
                if name == "label":
                    if a.fn is not ask_custom and value != a.label:
                        warnings.append(f"{path} agents.{key}: built-in agents keep their label")
                    continue
                if name == "command":
                    if a.fn is not ask_custom:
                        warnings.append(f"{path} agents.{key}: \"command\" is only for custom agents")
                        continue
                    a.exe = value[0]
                if name == "cost" and value not in COSTS:
                    warnings.append(f"{path} agents.{key}: unknown cost {value!r} (use {', '.join(COSTS)})")
                    continue
                if name == "budget":
                    value = _budget(value, f"{path} agents.{key}.budget", warnings)
                setattr(a, name, value)

    # Labels become heading and @mention syntax, so they must be distinct words.
    seen = {"claude", "all"}
    for key, a in list(agents.items()):
        if not LABEL_OK.match(a.label) or a.label.lower() in seen or key in seen:
            warnings.append(f"agent {key!r}: label {a.label!r} is invalid or taken; agent ignored")
            del agents[key]
            continue
        seen.update({a.label.lower(), key})
    return agents, defaults, loaded, warnings


def _budget(value: dict, where: str, warnings: list[str]) -> dict[str, int]:
    out = {}
    for name, limit in value.items():
        if name not in BUDGET_KEYS:
            warnings.append(f"{where}: ignored {name!r} (use {', '.join(BUDGET_KEYS)})")
        elif limit is None:
            continue
        elif isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            warnings.append(f"{where}: ignored {name!r}, expected a whole number >= 0 or null")
        else:
            out[name] = limit
    return out


class Usage:
    """Turn timestamps per agent, for the rolling per_day / per_week budgets.

    Shared by every council on this machine. Concurrent rounds can race and
    lose a count; that only makes the budget slightly generous.
    """

    def __init__(self, path: Path):
        self.path = path
        self.turns = self._load()

    def _load(self) -> dict[str, list[float]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            turns = data.get("turns", {})
            return {k: [float(t) for t in v] for k, v in turns.items() if isinstance(v, list)}
        except (OSError, ValueError, AttributeError, TypeError):
            return {}

    def count(self, key: str, seconds: float) -> int:
        cutoff = time.time() - seconds
        return sum(1 for t in self.turns.get(key, []) if t >= cutoff)

    def record(self, key: str) -> None:
        self.turns = self._load()  # pick up rounds that ran in parallel
        horizon = time.time() - max(s for s in BUDGET_KEYS.values() if s)
        self.turns[key] = [t for t in self.turns.get(key, []) if t >= horizon] + [time.time()]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"turns": self.turns}), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            print(f"[council] could not save budget usage to {self.path}: {exc}", file=sys.stderr)


def turns_left(agent: Agent, text: str, usage: Usage) -> tuple[int | None, str]:
    """(turns left under the tightest budget or None if unlimited, human summary)."""
    left, parts = None, []
    for name, limit in agent.budget.items():
        if BUDGET_KEYS[name] is None:
            used = sum(1 for m in headings(text) if m.group(1) == agent.label)
            where = "this discussion"
        else:
            used = usage.count(agent.key, BUDGET_KEYS[name])
            where = "24h" if name == "per_day" else "7d"
        parts.append(f"{max(limit - used, 0)}/{limit} {where}")
        left = limit - used if left is None else min(left, limit - used)
    return (None if left is None else max(left, 0)), ", ".join(parts)


def profile_lines(keys: list[str], text: str, usage: Usage) -> str:
    """The participants block of the prompt: strengths, cost and budget per agent."""
    lines = []
    for key in keys:
        a = AGENTS[key]
        bits = []
        if a.strengths:
            bits.append("best at " + ", ".join(a.strengths))
        if a.avoid:
            bits.append("weak at " + ", ".join(a.avoid))
        if a.cost:
            bits.append(f"budget {COSTS[a.cost]}")
        left, _ = turns_left(a, text, usage)
        if left is not None and left <= 2:
            bits.append(f"nearly out of budget ({left} turn{'s' if left != 1 else ''} left), "
                        "avoid @mentioning unless essential")
        if a.notes:
            bits.append(a.notes.rstrip("."))
        if bits:
            lines.append(f"- {a.label}: " + "; ".join(bits) + ".")
    if not lines:
        return ""
    return ("\nParticipants, as profiled by the user (use this to decide whom to @mention,\n"
            "and to weigh claims outside an agent's strengths more carefully):\n" + "\n".join(lines) + "\n")


AGENTS: dict[str, Agent] = {}
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
# A code span: a backtick run, then anything up to the same-length run, within one paragraph.
INLINE_CODE = re.compile(r"(?<!`)(`+)(?!`)((?:(?!\n[ \t]*\n).)*?)(?<!`)\1(?!`)", re.S)


def set_agents(agents: dict[str, Agent]) -> None:
    """Install the agent set, and the heading and @mention patterns built from its labels."""
    global AGENTS, LABELS, LABEL_KEY, HEADING, MENTION
    AGENTS = agents
    LABELS = ["Claude", *(a.label for a in agents.values())]
    LABEL_KEY = {a.label.lower(): k for k, a in agents.items()}
    alts = "|".join(re.escape(label) for label in LABELS)
    HEADING = re.compile(rf"^### ({alts}) \(round (\d+)(?:, follow-up \d+)?\)[ \t]*$", re.M)
    # "@codex", "@Grok," ... but not emails, paths or "x@grok".
    MENTION = re.compile(rf"(?<![\w@./\\-])@({alts}|all)(?![\w-])", re.I)


set_agents({k: Agent(k, label, k, fn) for k, (label, fn) in BUILTIN.items()})
NO_REPLY = re.compile(r"\(no reply: [^\n]*?(?:unavailable: (\w+))?\)")
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
    """Mention targets as agent keys, or "claude" / "all", in order of first use."""
    found = [LABEL_KEY.get(m.group(1).lower(), m.group(1).lower())
             for line, _ in prose_lines(text) for m in MENTION.finditer(line)]
    return list(dict.fromkeys(found))


def close_fences(reply: str) -> str:
    # An unclosed fence in one reply would hide every later heading from headings().
    fence = open_fence_at(reply)[1]
    return f"{reply}\n{fence}" if fence else reply


def unavailable_reason(error: str) -> str | None:
    """"quota" or "auth" when an agent's error output says retrying can't help, else None."""
    for reason, pattern in UNAVAILABLE:
        if pattern.search(error):
            return reason
    return None


def ask(agent: Agent, prompt: str, cwd: Path) -> tuple[str, float]:
    label = agent.label
    start = time.monotonic()
    timeout = agent.timeout
    for attempt in (1, 2):
        try:
            reply, code = agent.fn(prompt, cwd, timeout, agent)
            # Models sometimes echo their own heading; the script already writes it.
            reply = close_fences(re.sub(rf"^### {label}\b[^\n]*\n+", "", reply.lstrip()))
            if code:
                reply = f"(warning: {label} exited with code {code}; reply may be incomplete)\n\n{reply}"
            break
        except subprocess.TimeoutExpired:
            reply = f"(no reply: {label} timed out after {timeout}s)"
            break
        except Exception as exc:  # noqa: BLE001 — any failure becomes a visible transcript entry
            reason = unavailable_reason(str(exc))
            if reason:
                # Out of quota or logged out: a retry would fail the same way.
                reply = f"(no reply: {label} unavailable: {reason})\n{exc}"
                break
            reply = f"(no reply: {label} failed)\n{exc}"
            if attempt == 1 and RATE_LIMITED.search(str(exc)):
                print(f"[council] {label} rate limited, retrying once in {RATE_LIMIT_WAIT}s", file=sys.stderr, flush=True)
                time.sleep(RATE_LIMIT_WAIT)
                timeout = min(RETRY_TIMEOUT, agent.timeout)
                continue
            if attempt == 1 and time.monotonic() - start < FAST_FAIL:
                print(f"[council] {label} failed fast, retrying once: {str(exc)[:200]}", file=sys.stderr, flush=True)
                timeout = min(RETRY_TIMEOUT, agent.timeout)
                continue
            break
    took = time.monotonic() - start
    print(f"[council] {label} finished in {took:.0f}s", file=sys.stderr, flush=True)
    return reply, took


def status_of(reply: str) -> str:
    m = NO_REPLY.match(reply)
    if m:
        return f"UNAVAILABLE ({m.group(1)})" if m.group(1) else "NO-REPLY"
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


def check(cwd: Path, as_json: bool, loaded: list[str], warnings: list[str]) -> None:
    """--check: what's installed, how each agent is profiled and how much budget is left."""
    usage = Usage(config_paths(cwd)[0].with_name("usage.json"))
    user_path, project_path = config_paths(cwd)
    rows = []
    for key, a in AGENTS.items():
        left, summary = turns_left(a, "", usage)
        rows.append({
            "key": key, "label": a.label, "path": shutil.which(a.exe), "custom": a.fn is ask_custom,
            "enabled": a.enabled, "auto_join": a.auto_join, "cost": a.cost or None,
            "strengths": a.strengths, "avoid": a.avoid, "notes": a.notes, "has_instructions": bool(a.instructions),
            "budget": a.budget, "budget_left": summary or None, "timeout": a.timeout,
        })
    if as_json:
        print(json.dumps({
            "user_config": str(user_path), "project_config": str(project_path), "loaded": loaded,
            "warnings": warnings, "agents": rows, "suggested": SUGGESTED, "costs": list(COSTS),
            "budget_keys": list(BUDGET_KEYS),
        }, indent=2))
        return
    for path in (user_path, project_path):
        print(f"config: {path} ({'loaded' if str(path) in loaded else 'not found'})")
    for w in warnings:
        print(f"  warning: {w}")
    print()
    for r in rows:
        state = "NOT FOUND" if not r["path"] else r["path"]
        if not r["enabled"]:
            state = f"disabled in config ({state})"
        print(f"{r['label']:9} {state}")
        extras = []
        if r["cost"]:
            extras.append(f"cost {r['cost']}")
        if r["budget_left"]:
            extras.append(f"left {r['budget_left']}")
        if not r["auto_join"]:
            extras.append("joins only when @mentioned")
        if r["has_instructions"]:
            extras.append("has a role")
        if extras:
            print(f"{'':9} {' | '.join(extras)}")
        if r["strengths"]:
            print(f"{'':9} best at: {', '.join(r['strengths'])}")
        if r["avoid"]:
            print(f"{'':9} weak at: {', '.join(r['avoid'])}")
    if not loaded:
        print("\nNo config yet: every installed agent is used with no profile. "
              "Run with --init, or ask Claude for /agent-council:setup.")


def init(cwd: Path, force: bool) -> None:
    """--init: write a starter user config from SUGGESTED, enabling installed agents."""
    path = config_paths(cwd)[0]
    if path.exists() and not force:
        sys.exit(f"council.py: {path} already exists (use --force to overwrite)")
    agents = {}
    for key, (label, _) in BUILTIN.items():
        agents[key] = {
            "enabled": shutil.which(key) is not None, "auto_join": True, **SUGGESTED[key],
            "budget": {"per_discussion": None, "per_day": None, "per_week": None},
            "args": [], "instructions": "",
        }
    config = {
        "_comment": "agent-council config. cost: free | cheap | limited | expensive. budget: max turns, "
                    "null = unlimited. Project-specific strengths go in <repo>/.agent-council.json.",
        "defaults": {"hops": HOPS, "timeout": TIMEOUT},
        "agents": agents,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path}")


def main() -> None:
    global PROMPT_BUDGET
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
    ap.add_argument("--agents", help="comma-separated pool for messages without @mentions "
                    "(default: enabled agents with auto_join; env COUNCIL_AGENTS)")
    ap.add_argument("--cwd", type=Path, help="repository the agents inspect (default: git root of the current dir)")
    ap.add_argument("--hops", type=int,
                    help="follow-up turns agent @mentions may trigger per round (default 1; env COUNCIL_HOPS; 0 = none)")
    ap.add_argument("--check", action="store_true", help="show installed agents, profiles and budgets, then exit")
    ap.add_argument("--json", action="store_true", help="with --check: machine-readable output")
    ap.add_argument("--init", action="store_true", help="write a starter user config, then exit")
    ap.add_argument("--force", action="store_true", help="with --init: overwrite an existing config")
    args = ap.parse_intermixed_args()

    cwd = (args.cwd or git_root(Path.cwd())).resolve()
    agents, defaults, loaded, warnings = load_config(cwd)
    set_agents(agents)
    if args.init:
        init(cwd, args.force)
        return
    if args.check:
        check(cwd, args.json, loaded, warnings)
        return
    for w in warnings:
        print(f"[council] config warning: {w}", file=sys.stderr)
    if args.transcript is None:
        ap.error("transcript path is required")

    # Precedence: flag > environment > project config > user config > built-in default.
    hops = args.hops if args.hops is not None else \
        int(os.environ["COUNCIL_HOPS"]) if os.environ.get("COUNCIL_HOPS") else defaults.get("hops", HOPS)
    if not os.environ.get("COUNCIL_PROMPT_CHARS") and "prompt_chars" in defaults:
        PROMPT_BUDGET = defaults["prompt_chars"]
    pool = args.agents or os.environ.get("COUNCIL_AGENTS")
    if pool:
        wanted = [a.strip().lower() for a in pool.split(",") if a.strip()]
    elif "agents" in defaults:
        wanted = [a.lower() for a in defaults["agents"]]
    else:
        wanted = [k for k, a in AGENTS.items() if a.auto_join]
    unknown = [a for a in wanted if a not in AGENTS]
    if unknown:
        ap.error(f"unknown agent(s): {', '.join(unknown)} (choose from {', '.join(AGENTS)})")
    disabled = {key for key, a in AGENTS.items() if not a.enabled}
    installed = {key for key, a in AGENTS.items() if shutil.which(a.exe)}

    msg = " ".join(args.message) or sys.stdin.read()
    if not msg.strip():
        sys.exit("empty message")

    transcript = args.transcript
    text = transcript.read_text(encoding="utf-8-sig", errors="replace") if transcript.exists() else ""
    if not text:
        text = f"# Council transcript\n\nRepository: `{cwd}`\n"
    usage = Usage(config_paths(cwd)[0].with_name("usage.json"))

    def spent(key: str) -> bool:
        return turns_left(AGENTS[key], text, usage)[0] == 0

    # @mentions in the message pick who answers; none (or @all) means every selected agent.
    named = mentions(msg)
    targets = [m for m in named if m in AGENTS] if "all" not in named else []
    targets = targets or wanted
    for why, drop in (("disabled in config", disabled), ("not installed", AGENTS.keys() - installed)):
        skipped = [AGENTS[key].label for key in targets if key in drop]
        if skipped:
            print(f"[council] {why}, skipping: {', '.join(skipped)}", file=sys.stderr)
        targets = [key for key in targets if key not in drop]
    over = [key for key in targets if spent(key)]
    if over:
        print("[council] out of budget, skipping: "
              + ", ".join(f"{AGENTS[k].label} ({turns_left(AGENTS[k], text, usage)[1]})" for k in over),
              file=sys.stderr)
        targets = [key for key in targets if key not in over]
    if not targets:
        sys.exit("council.py: none of the selected agents are enabled, installed and within budget "
                 "(see --check)")

    _install_cleanup()
    transcript.parent.mkdir(parents=True, exist_ok=True)
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
    allowed = [key for key in AGENTS if key in installed and key not in disabled and not spent(key)
               and (key in wanted or key in targets)]
    participants = list(targets)
    print(f"[council] round {round_no}: asking {', '.join(AGENTS[k].label for k in targets)} in {cwd} "
          f"(follow-ups {hops})", file=sys.stderr, flush=True)

    before = worktree_fingerprint(cwd, transcript)
    latest: dict[str, tuple[str, float]] = {}  # agent -> (last reply, total seconds this round)
    to_claude: list[tuple[str, str]] = []
    unanswered: dict[str, list[str]] = {}
    batch = {key: TASK.format(name=AGENTS[key].label, round=round_no) for key in targets}
    hop = 0
    while batch:
        roster = ", ".join(["Claude", *(AGENTS[k].label for k in participants)])
        mentionable = ", ".join([*(f"@{AGENTS[key].label}" for key in allowed), "@Claude"])
        profiles = profile_lines(list(dict.fromkeys([*participants, *allowed])), text, usage)
        view = prompt_view(text, round_no, transcript)
        if view is not text and hop == 0:
            print(f"[council] transcript is {len(text)} chars; collapsed old replies to {len(view)} for the prompt",
                  file=sys.stderr)
        with ThreadPoolExecutor(len(batch)) as pool_:
            futures = [
                (key, pool_.submit(ask, AGENTS[key], PROMPT.format(
                    name=AGENTS[key].label, roster=roster, mentionable=mentionable, cwd=cwd,
                    role=f"\nYour role, set by the user: {AGENTS[key].instructions.strip()}\n"
                    if AGENTS[key].instructions.strip() else "",
                    profiles=profiles, transcript=view, task=task), cwd))
                for key, task in batch.items()
            ]
            results = [(key, *f.result()) for key, f in futures]

        suffix = f", follow-up {hop}" if hop else ""
        replies = "".join(f"\n### {AGENTS[key].label} (round {round_no}{suffix})\n\n{reply}\n"
                          for key, reply, _ in results)
        text += replies
        with transcript.open("a", encoding="utf-8") as fh:
            fh.write(replies)
        print(replies, flush=True)

        # Who got @mentioned, and by whom, in this batch.
        callers: dict[str, list[str]] = {}
        for key, reply, took in results:
            latest[key] = (reply, latest.get(key, ("", 0.0))[1] + took)
            if status_of(reply).startswith("UNAVAILABLE"):
                if key in allowed:
                    # Out of quota or logged out: don't offer or ask it again this round.
                    allowed.remove(key)
                    print(f"[council] {AGENTS[key].label} is {status_of(reply).lower()}; skipping it for the rest "
                          "of this round", file=sys.stderr)
            else:
                usage.record(key)  # a failed or timed-out turn may still have spent credits
                if key in allowed and spent(key):
                    allowed.remove(key)
                    print(f"[council] {AGENTS[key].label} has used up its budget", file=sys.stderr)
            if reply.startswith("(no reply"):
                continue
            label = AGENTS[key].label
            # Detect on the searchable copy, show the original so code spans survive.
            to_claude += [(label, original.strip()) for line, original in prose_lines(reply)
                          if any(m.group(1).lower() == "claude" for m in MENTION.finditer(line))]
            for m in mentions(reply):
                if m in AGENTS and m != key:
                    callers.setdefault(m, []).append(label)
        skipped = [AGENTS[k].label for k in callers if k not in allowed]
        if skipped:
            print(f"[council] mentioned but not available (disabled, not installed, not in the pool, "
                  f"out of quota or out of budget): {', '.join(skipped)}", file=sys.stderr)
        callers = {k: who for k, who in callers.items() if k in allowed}
        hop += 1
        if callers and hop > hops:
            unanswered = callers
            break
        batch = {key: FOLLOW_UP_TASK.format(callers=" and ".join(who), name=AGENTS[key].label, round=round_no)
                 for key, who in callers.items()}
        participants += [k for k in batch if k not in participants]
        if batch:
            print(f"[council] follow-up {hop}: asking "
                  + ", ".join(f"{AGENTS[k].label} (mentioned by {' and '.join(w)})" for k, w in callers.items()),
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
        print(f"--- not asked (follow-up limit {hops}): "
              + ", ".join(f"{AGENTS[k].label} <- {' and '.join(w)}" for k, w in unanswered.items()))
    summary = " | ".join(f"{AGENTS[key].label}: {status_of(reply)} ({took:.0f}s)"
                         for key, (reply, took) in latest.items())
    print(f"--- round {round_no} status: {summary}")
    budgets = [f"{a.label} {turns_left(a, text, usage)[1]}" for k, a in AGENTS.items()
               if a.budget and k in installed and k not in disabled]
    if budgets:
        print(f"--- budget left: {' | '.join(budgets)}")
    print(f"--- transcript: {transcript.resolve()}")


if __name__ == "__main__":
    main()
