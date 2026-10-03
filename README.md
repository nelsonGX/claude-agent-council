# agent-council

A group chat for your coding agents, inside Claude Code.

<img width="1920" height="1080" alt="image" src="https://github.com/user-attachments/assets/378397be-2d67-4d34-8046-bf30896de4b3" />

Claude asks, Codex, Grok and OpenCode argue it out in your repo, and Claude checks who's actually right before answering you.

## Install & Setup
Inside Claude Code, run
```
/plugin marketplace add nelsonGX/claude-agent-council
/plugin install agent-council@agent-council
```
After installation, you might want to setup customization for what, when, how agents has to act.
Please run 
```
/agent-council:setup
```
to pick your agents, set budgets, and decide when Claude should call the council.

You need Python 3.9+ and at least one of [`codex`](https://github.com/openai/codex), [`opencode`](https://opencode.ai) or `grok` installed and logged in.

## Use

Your Claude will run them automatically based on your perferences from the setup.
If you wanna run it manually, just ask for example:
> council this with codex and grok

Or run command `/agent-council:council`.

## Safety

Agents are told to only read your code (Codex and Grok are sandboxed; OpenCode and custom agents rely on the model behaving). If anything in the repo changes during a discussion, you'll get a warning.

## License

MIT
