---
name: cc-run
description: "Run a one-shot prompt, slash command or skill as another Claude account from ~/.claude-profiles, optionally on a named model. Use when the user hands a task to a named account: 'get <profile> to ...', 'have my work account do ...', 'run this as <profile>', 'ask <profile> using opus', or /cc-run <profile> <prompt>. NOT for switching the account VS Code and plain `claude` run as (cc-use)."
---

# Run a prompt as another account

1. Write the prompt, exactly as it should reach the other account, to `prompt.txt` in the
   scratchpad directory (or any temp file). A file sidesteps shell quoting for pasted text.
2. From the current working directory (so the run sees this project), run:

```sh
zsh -c 'source "${CLAUDE_PROFILES:-$HOME/.claude-profiles}/profiles.zsh"; cc "$@" -p' _ <profile> --permission-mode auto [--model <model>] < <path>/prompt.txt
```

Pass `--model` only when the user names one (`opus`, `sonnet`, `haiku`). Give the Bash call
a long timeout (up to 600000 ms) for anything beyond a quick answer. Sourcing
`profiles.zsh` inside the command matters: it runs the current `cc`, not a stale copy from
a shell snapshot, and keeps the guard and the skill/plugin sharing that a bare
`CLAUDE_CONFIG_DIR=... claude` would skip.

- **Skills and plugins:** `cc` links `~/.claude/skills` and `~/.claude/plugins` into the
  profile, so whatever resolves here resolves there. A plugin not installed on the default
  account is not available to the profile either.
- **Permissions:** a `-p` run cannot answer prompts, so it always runs in auto mode: the
  classifier approves safe edits and commands and blocks risky ones. Never swap in
  `bypassPermissions` or `dontAsk` unless the user asks for it.
- **Haiku has no auto mode.** On `--model haiku` the run falls back to asking, so every edit
  or command is denied. For a task that must act, say so and suggest the default model.
- **This session may block the launch** when it is itself in auto mode (the classifier's
  "Create Unsafe Agents" rule). Do not work around it: tell the user, who can approve it
  from manual mode or add a Bash allow rule for this command.
- **`cc` refuses the profile** because `cc-use` has it in the default slot: that login is
  the default one, so run the same command with `default` in place of the profile.
- **Not logged in:** the user runs `cc-login <profile>` in a terminal; it needs a browser.

## What to tell the user

Name the account (and model) that ran it in one short line, then give its output. When the
output is text for the user to paste somewhere, give the final text on its own, unwrapped.
