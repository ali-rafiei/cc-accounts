---
name: cc-usage
description: "Report Claude Code rate-limit usage across every Claude account on this Windows machine (the default login plus each profile under ~/.claude-profiles). Use when asked 'check my Claude usage', 'usage across all accounts', 'which account has headroom', 'am I near my limit', 'which account should I use', 'what's my weekly usage', or when picking which account to route work through. NOT for token or cost accounting of a single session."
---

# Claude multi-account usage (Windows)

```sh
python "$HOME/.claude-profiles/usage_table.py"
```

The same line works from Git Bash and PowerShell. If `python` is missing or opens the
Microsoft Store, use `py -3` in its place. If the script itself is missing, the commands
are not installed: point the user at `install.ps1` in the `windows` folder of this
plugin's repository. With a custom `CLAUDE_PROFILES`, use that directory instead.

Takes 10-30s (one `claude -p "/usage"` per account, six in parallel).

One row per account. A `*` on the profile marks the account this session runs as, and
`[default]` marks the profile `cc-use` has loaded into the default login (what VS Code and
plain `claude` run as). The default login gets a row named `default` only when it holds an
account no named profile covers, so no account is listed twice.

## Response style: low verbosity

Print the table verbatim **inside a fenced code block** (open with ```` ```text ````, close
with ```` ``` ````), every time. Outside a fence, Markdown collapses the column padding and
the table renders as run-on text. Add **nothing** else unless one of the cases below applies.
No column explanations, no summary of the numbers, no restating rows in prose.

## The only additions allowed

- **A row says `not logged in`**: one line, `` `cc-login <profile>` to re-auth ``, run in
  PowerShell by the user; it needs a browser.
- **The user asked which account to use**: name one account in one line. Pick the lowest
  `WEEK`, ignoring accounts whose reset is imminent, and check `OTHER LIMITS` too: a
  per-model cap at 100% makes that model unusable even when `WEEK` is low.
- **The script fails or a row shows `no limit data`**: say what broke, briefly.
- **A row carries `*` or `[default]`**: no comment needed.

## Adding an account

The user runs `cc-add <profile>` in PowerShell. The login is an interactive browser flow,
so it cannot be completed from a non-interactive session.
