# agent-council

A Claude Code plugin that lets Claude hold a group discussion with the other coding agents you already have installed — **Codex**, **OpenCode** and **Grok** — before it commits to a conclusion.

Every agent reads the same Markdown transcript, inspects your repo **read-only** with its own tools, cites `file:line` for its claims and ends with `Status: AGREE | DISAGREE | NEED-INFO`. Claude moderates and verifies claims in the code instead of going with the majority.

It works like a group chat:

- Claude @mentions who should answer: `@codex @grok is this retry idempotent?`
- Agents @mention each other. `@Codex your claim at foo.py:40 is wrong` gives Codex a follow-up turn in the same round.
- Agents @mention Claude. Those lines are collected at the end of each round so Claude answers them.

## Requirements

- Claude Code
- Python 3.9+ (standard library only)
- At least one of these CLIs, logged in: [`codex`](https://github.com/openai/codex), [`opencode`](https://opencode.ai), `grok`, or any other CLI you add as a custom agent

## Install

```
/plugin marketplace add nelsonGX/claude-agent-council
/plugin install agent-council@agent-council
```

To use a local checkout instead, run `/plugin marketplace add <path-to-this-repo>`.

## Use

Ask Claude for it: "council this with codex and grok", "get a second opinion on this plan", or invoke `/agent-council:council`.

To check which agents the script can find, ask Claude to "run the council check". From a clone of this repo you can also run it yourself:

```
python3 plugins/agent-council/scripts/council.py --check
```

On Windows, use `python` if `python3` isn't found or opens the Microsoft Store.

## Customize your council

Run **`/agent-council:setup`** and Claude walks you through it: which agents take part, how much each one costs you, what each is best at. It then writes the config.

It also asks **when** you want the council used: before big changes, for reviews and audits, for planning implementation scope, for root-cause claims, for sensitive code, or only when you ask. It also asks whether Claude should run it automatically or check with you first. Your answer is saved as a small marked section in your global `~/.claude/CLAUDE.md`, or in the project's `CLAUDE.md`, `CLAUDE.local.md` or `AGENTS.md`. Re-running setup updates that section in place. Claude shows you the section before writing it. To start from defaults by hand, run `python3 plugins/agent-council/scripts/council.py --init` and edit the file it prints.

| Setting | What it does |
| --- | --- |
| `enabled` | Turn an agent off without uninstalling it. |
| `cost` | `free` / `cheap` / `limited` / `expensive`. The agents and Claude are told how freely to @mention it. |
| `budget` | Hard caps on turns: `per_discussion`, `per_day` (rolling 24 h), `per_week`. An agent that runs out is skipped and listed as out of budget. |
| `strengths` / `avoid` | What it's good or weak at, e.g. Codex for backend and audits, Grok for security and web lookups. Claude routes questions to it on that basis, and so do the other agents. |
| `auto_join` | `false` means it answers only when Claude @mentions it. Good for expensive agents. |
| `instructions` | A private role for that agent, e.g. "be the security red team". |
| `args` / `timeout` | Extra CLI flags (usually a model) and a per-turn time limit. |
| `command` | Add **any other CLI** as a custom agent. |

Example (`%APPDATA%\agent-council\config.json` on Windows, `~/.config/agent-council/config.json` elsewhere; `--check` prints the exact path):

```json
{
  "agents": {
    "codex": { "cost": "limited", "budget": { "per_discussion": 8, "per_day": 40 },
               "strengths": ["backend and APIs", "deep code audits", "tests"] },
    "grok":  { "cost": "free", "strengths": ["security review", "devil's advocate", "web lookups"],
               "instructions": "Act as the security red team." },
    "opencode": { "enabled": false },
    "gemini": { "label": "Gemini", "command": ["gemini", "-p", "Follow the instructions in {prompt_file}"],
                "cost": "cheap", "auto_join": false, "strengths": ["large-context reading"] }
  }
}
```

A repo can add an `.agent-council.json` with project-specific `strengths`, `avoid`, `notes`, `instructions`, `enabled` and `auto_join`. Commands, args, cost and budgets are ignored there, so a cloned repo can't run programs on your machine or change your budgets.

## Troubleshooting

- **An agent shows `NOT FOUND` but works in your terminal.** The script finds agents on the `PATH` it inherits from Claude Code. If you started Claude Code from the Dock, Finder or a desktop launcher, that `PATH` may be missing directories your shell profile adds, such as `/opt/homebrew/bin`, `~/.local/bin` or an npm/fnm/nvm global bin. Start Claude Code from a terminal, or make those directories available to GUI apps.
- **An agent shows `UNAVAILABLE (quota)` or `UNAVAILABLE (auth)`.** Its CLI reported that it's out of credits or not logged in. Fix that in the agent's own CLI. The council keeps going with the others and tries it again next round.

The full moderation protocol, flags and environment variables are documented in [`skills/council/SKILL.md`](plugins/agent-council/skills/council/SKILL.md).

## Safety

Agents run read-only, but how that's enforced differs by agent:

- Codex runs in its enforced read-only sandbox.
- Grok gets only read and search tools.
- OpenCode runs its `plan` agent, which still has a shell, so it relies on the model complying.
- Custom agents get no sandbox from the script. Use the CLI's own read-only or plan mode.

After every round the script compares `git status` and `git diff` from before and after the round, and writes a warning into the transcript if the working tree changed. That check can't see ignored files or writes outside the repo.

## License

MIT
