---
name: cc-run
description: "Run a one-shot prompt, slash command or skill as another Claude account from ~/.claude-profiles on Windows, optionally on a named model. Use when the user hands a task to a named account: 'get <profile> to ...', 'have my work account do ...', 'run this as <profile>', 'ask <profile> using opus', or /cc-run <profile> <prompt>. NOT for switching the account VS Code and plain `claude` run as (cc-use)."
---

# Run a prompt as another account (Windows)

1. Write the prompt, exactly as it should reach the other account, to `prompt.txt` in the
   scratchpad directory (or any temp file). A file sidesteps shell quoting for pasted text.
2. From the current working directory (so the run sees this project), run:

```sh
python "$HOME/.claude-profiles/cc_run.py" run <profile> --prompt-file <path>/prompt.txt --permission-mode auto [--model <model>] -p
```

The same line works from Git Bash and PowerShell (which has no `<` redirect, hence
`--prompt-file`, which must come straight after the profile). Use `py -3` if `python` is
missing, and a custom `CLAUDE_PROFILES` directory in place of `~/.claude-profiles`. Pass
`--model` only when the user names one (`opus`, `sonnet`, `haiku`). Give the call a long
timeout (up to 600000 ms) for anything beyond a quick answer.

- **Skills and plugins:** `cc_run.py` gives the profile junctions to `~/.claude/skills` and
  `~/.claude/plugins`, so whatever resolves here resolves there. A plugin not installed on
  the default account is not available to the profile either.
- **Permissions:** a `-p` run cannot answer prompts, so it always runs in auto mode: the
  classifier approves safe edits and commands and blocks risky ones. Never swap in
  `bypassPermissions` or `dontAsk` unless the user asks for it.
- **Haiku has no auto mode.** On `--model haiku` the run falls back to asking, so every edit
  or command is denied. For a task that must act, say so and suggest the default model.
- **This session may block the launch** when it is itself in auto mode (the classifier's
  "Create Unsafe Agents" rule). Do not work around it: tell the user, who can approve it
  from manual mode or add an allow rule for this command.
- **It refuses the profile** because `cc-use` has it in the default slot: that login is the
  default one, so use `run default` instead.
- **Not logged in:** the user runs `cc-login <profile>` in PowerShell; it needs a browser.

## What to tell the user

Name the account (and model) that ran it in one short line, then give its output. When the
output is text for the user to paste somewhere, give the final text on its own, unwrapped.
