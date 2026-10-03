---
name: council
description: Hold a back-and-forth group discussion with other coding agents (Codex, OpenCode, Grok) over a shared transcript before committing to a conclusion. Use for non-trivial work in any repo — planning a new feature or cross-module change, audits and reviews (security, billing, permissions, state machines, protocols), or before claiming "X is the cause / unused / safe / works like Y" without having verified it in the code. Also use when the user says "council", "ask codex/grok/opencode", "@codex", "get a second opinion" or "discuss with the other agents". Skip for trivial edits and questions already answered by reading the code.
---

# Council

A single agent tends to trust its own first idea and state guesses about the code as fact. The council sends the same transcript to several other agents, which reply to you and to each other like a group chat, so claims get checked before you act on them.

## Running a round

Keep one transcript file per topic in your scratchpad directory (or a temp dir if there is none):

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/council.py" <scratchpad>/council-<topic>.md "<your message for this round>"
```

On Windows, use `python` if `python3` isn't found or opens the Microsoft Store. If the user asks for "the council check", or an agent unexpectedly isn't installed, run the script with `--check` and no transcript. If an agent the user has installed shows `NOT FOUND`, it's probably missing from the `PATH` Claude Code inherited (for example, it was launched from a GUI); tell the user that.

**Run it in the background** (`run_in_background: true` on the Bash call) and keep working — read the code you'll need to verify their claims. You are notified when the round ends; then read the output. Don't start another round on the same transcript until it has finished.

### Addressing: @mentions

- **You pick who answers** by @mentioning them in your message: `@codex @grok is the retry in client.py:88 idempotent?` asks only Codex and Grok. No mention, or `@all`, asks every selected agent. Mentions inside code spans, fenced blocks and `>` quotes don't count.
- **Agents can @mention each other.** If Grok writes `@Codex your claim at foo.py:40 is wrong`, Codex gets a follow-up turn in the same round (heading `### Codex (round N, follow-up 1)`). `--hops N` (env `COUNCIL_HOPS`, default 1) caps how many follow-up waves a round may trigger; `--hops 0` disables them. An agent can only pull in agents from the `--agents` pool or ones you mentioned, and `@all` works only in your message. Mentions over the limit are listed as `--- not asked …`; @mention those agents yourself next round if the point matters.
- **Agents can @mention you.** Every line addressed to @Claude is listed at the end of the output under `--- addressed to @Claude:`. Answer each one in your next message (or pass it to the user if it's about product intent) — don't let them drop.

### Details

- Each call appends your message and sends the full transcript to the addressed agents in parallel. It then appends their replies, prints them and ends with a status line such as `Codex: AGREE (84s) | Grok: DISAGREE (40s)` (an agent's latest reply in the round counts).
- The agents work in the **git root of the current directory**. Pass `--cwd <path>` when the code under discussion is elsewhere, or the wrong tree gets reviewed without any error.
- Read-only enforcement differs by agent. These results come from write tests on Windows:
  - **Codex** runs with `--sandbox read-only -c approval_policy="never"`. The sandbox is enforced.
  - **Grok** runs with `--tools read_file,grep,list_dir,web_search,web_fetch`, so it has no shell and no write tool. Grok can't run `git log` or scripts in a council.
  - **OpenCode** runs with `--agent plan`, which denies edits but still allows its shell, so it stays read-only only because the model complies.
  - The script fingerprints `git status` and `git diff HEAD` (not counting the transcript) before and after each round, and appends a **WARNING** if the working tree changed. That check can't see ignored files, writes outside the repo, or directories that aren't git repos.
- Grok has claimed to have written a file it never wrote. Treat any agent's claim about something it ran or did the same as its claims about code: unverified until you check.
- Extra CLI flags per agent: `COUNCIL_CODEX_ARGS`, `COUNCIL_OPENCODE_ARGS`, `COUNCIL_GROK_ARGS` (for example `-m grok-4.7-build-fast` when Grok is too slow).
- `--agents codex,grok` sets the default pool for messages without mentions (env `COUNCIL_AGENTS`). By default the script uses every installed agent and skips missing ones with a notice. `--check` shows which are installed.
- For long messages, or ones with quotes or backticks, pipe the message on stdin, for example with a heredoc (`<<'EOF'`), instead of passing it as an argument.
- A turn takes 1–6 minutes, Grok is usually the slowest, and follow-ups add another turn. `COUNCIL_TIMEOUT` (default 420 s) is the per-agent limit per turn. An agent that fails within 60 s is retried once with a 90 s limit, and a rate-limited one (429) once after 20 s. An agent whose error says it is out of quota/credits or logged out is not retried: it shows as `UNAVAILABLE (quota)` or `UNAVAILABLE (auth)`, is skipped for the rest of the round (including follow-ups and the @mention list) and is tried fresh next round. Tell the user which agent is unavailable and why; don't treat its silence as agreement. A reply prefixed `(warning: Grok stopped with an error …)` is partial work kept from a turn that failed midway. If you must run in the foreground, give the Bash call its maximum timeout and use `--hops 0`.
- Agent process trees are killed on timeout, and also if the script itself is killed (on Windows through a kill-on-close Job Object, on Unix through signal handlers, which can't catch SIGKILL).
- If a previous call died before any reply was written, rerunning the same transcript replaces that unanswered round instead of duplicating it.
- Once the transcript passes about 60k characters (`COUNCIL_PROMPT_CHARS`), agent replies older than the previous round are collapsed to their status line in the prompt only. The file keeps everything, and your own messages are always sent in full, so restate anything important in your latest message.
- Progress (`[council] Grok finished in 40s`, `[council] follow-up 1: asking Codex (mentioned by Grok)`) goes to stderr. A reply prefixed with `(warning: … exited with code N)` may be incomplete.

## Moderating (you are the moderator, not just another voice)

1. **Round 1: independent answers.** State the goal, the relevant paths and the constraints. Ask for their plan or findings. Don't give your own conclusion yet, so their answers aren't anchored to yours.
2. **Round 2 and later: debate.** Add your own view and what you verified yourself. Name the specific disagreements and @mention the agents who must defend or drop a point, for example: "@codex respond to Grok's claim X." Answer every line addressed to @Claude. Bring in new questions as they come up.
3. **Stop** when everyone reaches `Status: AGREE` (which means the agent checked every code claim it relies on and has no remaining objection), when the remaining disagreement comes down to product intent (hand that to the user), or after about 4 rounds.

## Using what they say

- Treat every claim as unverified until you have read the cited `file:line` yourself, both before you act on it and before you repeat it. The agents hallucinate too.
- Settle disagreements by reading the code, not by majority vote.
- Tell the user briefly who said what, where you disagreed, how you settled it and what is still open. Point them to the transcript path.
- If an agent fails or times out, the transcript shows `(no reply …)`. Say so, and carry on with the others. Never claim a review happened when it didn't.
