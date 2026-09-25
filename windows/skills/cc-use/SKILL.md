---
name: cc-use
description: "Switch which Claude account the default login runs as on Windows (VS Code, plain `claude`, every session started without a profile) by loading a ~/.claude-profiles profile's login into it, or put the user's own login back. Use when asked 'switch VS Code to <profile>', 'use my work account', 'swap accounts', 'switch account', 'switch to whichever has headroom', 'put my account back', 'which account is VS Code on', or /cc-use <profile>. NOT for running one prompt as another account (cc-run) or for checking usage (cc-usage)."
---

# Switch the default login (Windows)

```sh
python "$HOME/.claude-profiles/cc_use.py"              # which account is loaded
python "$HOME/.claude-profiles/cc_use.py" <profile>    # load that profile's login
python "$HOME/.claude-profiles/cc_use.py" default      # put the user's own back
```

The same lines work from Git Bash and PowerShell; use `py -3` if `python` is missing, and a
custom `CLAUDE_PROFILES` directory in place of `~/.claude-profiles`. The
argument is a profile directory name under `~/.claude-profiles/`, or `default`. No argument
means status. If the script is missing, point the user at `install.ps1` in the `windows`
folder of this plugin's repository.

**"Whichever has headroom" / "the best one":** run the `cc-usage` table first, then load the
account with the lowest `WEEK`, skipping any whose reset is imminent or whose `OTHER LIMITS`
shows a model at 100% that the user needs.

## What to tell the user

One line: the command's own output line. It already says what moved: new sessions start on
the new account at once, running ones pick it up on their next message, and reopening the
Claude tab in VS Code makes sure.

Say once, the first time in a conversation, that **every** session on the default login
switches, this one and all VS Code tabs included.

On a `cc-use:` error, relay the line; it names the cause. The refusals are deliberate:

- **A running session on that profile**: two live copies of one login strand one of them.
  The user exits it, or picks another profile.
- **The default slot holds a different account than recorded**: something wrote the login
  behind cc-use's back. Do not force it; tell the user and stop.
- **Could not confirm whose login it is**: offline, or an expired access token. One message
  in any default session refreshes it; then retry.
- **A file stayed locked by another program**: something (often antivirus or a sync tool)
  had the credentials file open. Retry in a moment.

## How it works (for troubleshooting)

On Windows, Claude Code keeps each login as a file: `~/.claude/.credentials.json` for the
default login, `<profile>/.credentials.json` for a profile. `cc_use.py` moves that file's
contents and `oauthAccount` in `~/.claude.json`, writing the outgoing login back to its owner
first. The user's own login waits in `~/.claude-profiles/.home-credentials.json` while another
is loaded. The loaded profile is recorded in `~/.claude-profiles/.loaded`; while it is set,
`cc <that profile>` refuses and the usage table reads that profile through the default login.
