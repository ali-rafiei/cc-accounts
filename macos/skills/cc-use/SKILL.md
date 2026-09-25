---
name: cc-use
description: "Switch which Claude account the default login runs as (VS Code, plain `claude`, every session started without a profile) by loading a ~/.claude-profiles profile's login into it, or put the user's own login back. macOS only. Use when asked 'switch VS Code to <profile>', 'use my work account', 'swap accounts', 'switch account', 'switch to whichever has headroom', 'put my account back', 'which account is VS Code on', or /cc-use <profile>. NOT for running one prompt as another account (cc-run) or for checking usage (cc-usage)."
---

# Switch the default login

```sh
python3 "${CLAUDE_PROFILES:-$HOME/.claude-profiles}/cc_use.py"              # which account is loaded
python3 "${CLAUDE_PROFILES:-$HOME/.claude-profiles}/cc_use.py" <profile>    # load that profile's login
python3 "${CLAUDE_PROFILES:-$HOME/.claude-profiles}/cc_use.py" default      # put the user's own back
```

The argument is a profile directory name under `~/.claude-profiles/`, or `default`. No
argument means status. If the script is missing, the shell side is not installed: point
the user at `install.sh` in this plugin's repository.

**"Whichever has headroom" / "the best one":** run the `cc-usage` table first, then load the
account with the lowest `WEEK`, skipping any whose reset is imminent or whose `OTHER LIMITS`
shows a model at 100% that the user needs.

## What to tell the user

One line: the command's own output line. It already says what moved and that running
sessions pick it up within ~30s (reopening the Claude tab in VS Code switches at once).

Say once, the first time in a conversation, that **every** session on the default login
switches, this one and all VS Code tabs included.

On a `cc-use:` error, relay the line; it names the cause. The refusals are deliberate:

- **A running session on that profile**: two live copies of one login strand one of them.
  The user exits it, or picks another profile.
- **The default slot holds a different account than recorded**: something wrote the login
  behind cc-use's back. Do not force it; tell the user and stop.
- **Could not confirm whose login it is**: offline, or an expired access token. One message
  in any default session refreshes it; then retry.

## How it works (for troubleshooting)

`cc_use.py` moves the `Claude Code-credentials` Keychain item and `oauthAccount` in
`~/.claude.json`, writing the outgoing login back to its owner first. The user's own login
waits in the `Claude Code-credentials-cc-use-home` item while another is loaded. The loaded
profile is recorded in `~/.claude-profiles/.loaded`; while it is set, `cc <that profile>`
refuses and the usage table reads that profile through the default login.
