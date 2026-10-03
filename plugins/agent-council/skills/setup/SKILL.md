---
name: setup
description: Interactively configure the agent council — which agents take part, each agent's cost tier and turn budget (so it gets @mentioned more or less), what each agent is best at, private roles, models/extra CLI flags, and custom agents such as another CLI — plus a "when to use the council" policy written into the user's global or project CLAUDE.md / AGENTS.md. Use when the user says "set up the council", "configure council agents", "council setup", "change Codex's budget", "Grok is free now", "add gemini to the council", "always council before big changes", "use the council for reviews", "stop using the council so often", or invokes /agent-council:setup.
---

# Council setup

You are configuring the agent council for this user. There are two outputs:

- a JSON config, which the council script reads every round: who takes part, cost, budgets, strengths;
- a **council policy**: a short section in CLAUDE.md / AGENTS.md that says *when* the council should be used.

Interview the user, propose good defaults, write both, and verify them. If the user asks about only one part ("council before every big change", "Codex is out of credits"), do only that part.

## 1. See what's there

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/council.py" --check --json
```

(On Windows use `python` if `python3` isn't found.) The output gives you:

- `user_config` and `project_config`: the file paths, and `loaded`: which of them exist.
- `agents`: every known agent with its install path (`null` = not on PATH) and its current profile.
- `suggested`: the default profiles to propose for the built-in agents.
- `warnings`: problems in an existing config. Mention them and fix them while you're at it.

If a user config already exists, read it first and treat this as an edit: ask only about what the user wants to change, and keep everything else.

## 2. Interview

Use AskUserQuestion. Batch related questions (up to 4 per call), keep the number of calls small, and put your recommendation first. Skip questions the user already answered in their request ("Grok is free, Codex is limited" answers the cost question for both).

1. **Which agents take part** (multiSelect). List the installed ones. Mention any agent that isn't installed, and offer to add a custom CLI agent (see step 4).
2. **Cost tier per agent.** This tells the agents and you how freely to @mention it:
   - `free`: @mention freely, good for broad sweeps and long debates.
   - `cheap`: @mention when useful.
   - `limited`: a subscription with usage caps. Save it for questions that match its strengths.
   - `expensive`: pay-per-use or scarce. Use it only for decisive questions.
3. **Turn budgets** (hard caps the script enforces; one turn = one reply, follow-ups included). Offer presets, and let the user type exact numbers with "Other":
   - free: no caps
   - limited: `per_discussion` 8, `per_day` 40
   - expensive: `per_discussion` 3, `per_day` 10
   Windows: `per_discussion` (one transcript), `per_day` (rolling 24 h), `per_week` (rolling 7 days). Use `null` for no cap.
4. **What each agent is best at.** Propose `suggested[key].strengths` plus anything you know about the model the user names. The user can accept, edit or replace them. Also ask about `avoid` (what it's weak at) only if the user has opinions. Keep the entries short, at most about 5 per agent.
5. **Join behaviour.** `auto_join: false` means the agent never joins a round on its own: it answers only when you @mention it, and other agents can't pull it in. Recommend this for `expensive` agents.
6. **Optional extras.** Ask about these only if relevant or requested:
   - `instructions`: a private role for that agent, for example "act as the security red team" or "argue the opposite of the majority".
   - `args`: extra CLI flags, usually a model (`["-m", "grok-4.7-build-fast"]`).
   - `timeout` in seconds.
   - `defaults.hops` (follow-up waves per round) and `defaults.agents` (the default pool).

## 3. Write the config

Write the user config to `user_config` (create the directory if needed), as JSON in this shape. Leave out fields the user didn't set:

```json
{
  "defaults": { "hops": 1, "timeout": 420 },
  "agents": {
    "codex": {
      "enabled": true,
      "auto_join": true,
      "cost": "limited",
      "budget": { "per_discussion": 8, "per_day": 40, "per_week": null },
      "strengths": ["backend and APIs", "deep code audits", "tests"],
      "avoid": ["UI copy"],
      "notes": "Slow but precise.",
      "instructions": "",
      "args": ["-m", "gpt-5.5"],
      "timeout": 600
    },
    "grok": { "cost": "free", "strengths": ["security review", "devil's advocate", "web lookups"] }
  }
}
```

**Project-specific strengths** (for example "Codex knows our Rust core best") go in `<repo>/.agent-council.json` instead. That file may set only `enabled`, `auto_join`, `strengths`, `avoid`, `notes` and `instructions` per agent, plus `defaults.agents` and `defaults.hops`. Cost, budgets, args and commands stay in the user config, so a cloned repo can't run programs or change budgets. Ask the user before creating a file inside their repo, since it may get committed.

## 4. Custom agents

Any CLI that takes a prompt and prints a reply can join. Add it to the **user config** with a `command`:

```json
"gemini": {
  "label": "Gemini",
  "command": ["gemini", "-p", "Follow the instructions in {prompt_file}"],
  "cost": "free",
  "strengths": ["large-context reading", "docs"]
}
```

- `{prompt_file}` is replaced with the path to a file holding the prompt. Without it, the prompt goes to stdin. `{cwd}` is replaced with the repo path.
- The reply is whatever the command prints on stdout, so pick a non-interactive, plain-text mode.
- `label` is how it's addressed (`@Gemini`). It must be one word and can't clash with another agent's label or with Claude.
- **Read-only is not enforced for custom agents.** Use the CLI's own read-only or plan mode if it has one, and tell the user that only the post-round `git status` check backs it up.
- Check the CLI's real flags (`<cli> --help`) before writing the command. Don't guess.

## 5. When to use the council (memory policy)

The council skill triggers on its own description, which is generic. A policy in the user's memory files makes it match *their* habits, for example "always council before a big change" or "only when I ask".

**Ask** (AskUserQuestion, multiSelect, recommendation first; skip whatever the user already said):

1. **When should the council run?**
   - Before big or cross-module changes, migrations, or public API changes (recommended)
   - Reviews and audits, before saying work is done or safe
   - Planning implementation scope and approach for a new feature
   - Root-cause claims while debugging ("X is the cause")
   - Security-, auth-, billing- or data-loss-sensitive code
   - Only when I explicitly ask
2. **How should Claude start one?** Run it automatically, or ask the user first with a one-line reason.
3. **Where should the policy live?** Offer only what applies:
   - **Global** `~/.claude/CLAUDE.md`: all projects, personal (recommended for habits).
   - **Project** `<repo>/CLAUDE.md`: shared with the team, committed.
   - **Project, personal** `<repo>/CLAUDE.local.md`: this repo only, usually gitignored.
   - **`<repo>/AGENTS.md`**: read by Codex, OpenCode and other agents too. Use it when the user runs other agents as the *main* agent and wants the same policy there. Only Claude Code has this plugin, so phrase the policy as "ask for a council / second opinion" rather than naming the script.

**Write** a managed block, so that re-running setup replaces it instead of adding a duplicate. Build it only from the user's answers, for example:

```markdown
<!-- agent-council:policy start (managed by /agent-council:setup; edit freely or re-run setup) -->
## Agent council

Use the council skill (agent-council:council) — ask me first with a one-line reason — when:
- planning a change that spans several modules, a migration, or a public API change;
- reviewing or auditing work before calling it done, safe or correct;
- about to state a root cause without having verified it in the code.

Route by the council config: free agents for broad sweeps; Codex for backend/audit
questions; Grok for security. Skip it for trivial edits and questions the code answers directly.
If you are yourself a participant in a council, ignore this section.
<!-- agent-council:policy end -->
```

Rules for editing these files:

- **Read the file first.** If the markers exist, replace only the text between them. If there are no markers but there's an older hand-written council section, ask whether to replace it or leave it. Otherwise append the block at the end. Never rewrite other parts of the file.
- Keep it short (under about 15 lines). These files load into every session.
- Don't add the routing line unless a config with strengths exists. It should restate the config, not contradict it.
- Keep the participant line ("If you are yourself a participant…"). Codex and OpenCode read AGENTS.md and CLAUDE.md, and the script already refuses to run inside a participant, but the line also saves them a wasted attempt.
- Show the user the block, and the file it's going into, before writing. Committed files (`CLAUDE.md`, `AGENTS.md`) affect teammates, so say so and get a yes. To remove the policy ("stop using the council automatically"), delete the block, markers included.
- If both a global and a project policy exist, the project one is more specific. Mention that if they conflict.

## 6. Verify and report

Run `--check` (without `--json`) again and show the user the result. Confirm there are no warnings, that every enabled agent is found, and that budgets and strengths look right. Tell them:

- which memory file(s) got the council policy, if any,

- where the config lives and that they can re-run `/agent-council:setup` or edit the file to change it,
- that budgets count real turns. Usage is kept in `usage.json` next to the config; deleting it resets the rolling windows.
