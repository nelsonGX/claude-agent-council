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
- At least one of these CLIs, logged in: [`codex`](https://github.com/openai/codex), [`opencode`](https://opencode.ai), `grok`

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

## Troubleshooting

- **An agent shows `NOT FOUND` but works in your terminal.** The script finds agents on the `PATH` it inherits from Claude Code. If you started Claude Code from the Dock, Finder or a desktop launcher, that `PATH` may be missing directories your shell profile adds, such as `/opt/homebrew/bin`, `~/.local/bin` or an npm/fnm/nvm global bin. Start Claude Code from a terminal, or make those directories available to GUI apps.
- **An agent shows `UNAVAILABLE (quota)` or `UNAVAILABLE (auth)`.** Its CLI reported that it's out of credits or not logged in. Fix that in the agent's own CLI. The council keeps going with the others and tries it again next round.

The full moderation protocol, flags and environment variables are documented in [`skills/council/SKILL.md`](plugins/agent-council/skills/council/SKILL.md).

## Safety

Agents run read-only, but how that's enforced differs by agent:

- Codex runs in its enforced read-only sandbox.
- Grok gets only read and search tools.
- OpenCode runs its `plan` agent, which still has a shell, so it relies on the model complying.

After every round the script compares `git status` and `git diff` from before and after the round, and writes a warning into the transcript if the working tree changed. That check can't see ignored files or writes outside the repo.

## License

MIT
