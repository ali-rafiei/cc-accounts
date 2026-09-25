# claude-multi-account for macOS

Run Claude Code as several accounts on one machine. Each account gets its own profile,
one command shows usage across all of them, Claude can hand a task to another account, and
on macOS you can switch which account VS Code and plain `claude`
in a terminal run as, without reloading anything.

Written for people who have more than one Claude subscription or seat (personal and work,
or several team seats) and want to use whichever one has room left this week.

## What you get

Shell commands (zsh):

| Command | What it does |
|---|---|
| `cc-add <profile>` | Create a profile and log it in |
| `cc <profile> [args]` | Run Claude Code as that account (`cc default` runs your normal login) |
| `cc-login <profile>` | Log a profile in again. The sign-in page opens in a new private browser window on a throwaway profile (Google Chrome, or the browser `CC_LOGIN_BROWSER` names), so it can't pick up whichever account your browser, or an earlier login, is signed into |
| `ccusage-all` | One table of session and weekly usage for every account, soonest reset first |
| `cc-use <profile>` | macOS: put that account's login in the default slot, so VS Code and plain `claude` run as it. `cc-use default` puts yours back |

Skills, so you can ask Claude directly:

| Skill | Say something like |
|---|---|
| `cc-usage` | "check my Claude usage", "which account has headroom?" |
| `cc-run` | "have work run the test suite", "ask personal, using opus, to review this" |
| `cc-use` | "switch VS Code to work", "switch to whichever account has room", "put my account back" |

```text
ACCOUNT             PROFILE           SESSION  WEEK  OTHER LIMITS  WEEK RESETS
------------------  ----------------  -------  ----  ------------  ---------------
work@example.com    work [default] *  12%      31%   Opus 18%      Saturday at 3am
me@example.com      personal          0%       64%   Opus 70%      Tuesday at 1pm
team-2@example.com  team-2            0%       2%    Opus 0%       Thursday at 7pm
```

`*` marks the account the current session runs as. `[default]` marks the profile `cc-use`
has loaded into your default login.

## Requirements

- Claude Code (the `claude` CLI) with your normal account already logged in
- zsh and Python 3.9+. On macOS, zsh is the default shell and `/usr/bin/python3` is enough
  once the Command Line Tools are installed.
- macOS for `cc-use` and for `cc-login`'s throwaway browser window. The rest only needs zsh, so it
  should work on Linux, but it has only been tested on macOS.
- Google Chrome for that window, or set `CC_LOGIN_BROWSER` to the app name of Firefox or
  another Chromium-based browser (`Microsoft Edge`, `Brave Browser`). Safari has no
  command-line private-window option, so it can't be used.

## Install

The skills need the shell commands, so install those first:

```sh
git clone https://github.com/ali-rafiei/claude-multi-account.git
cd claude-multi-account/macos
./install.sh
```

`install.sh` copies four files into `~/.claude-profiles` (or `$CLAUDE_PROFILES`) and adds
one `source` line to `~/.zshrc` (or `$ZDOTDIR/.zshrc`) unless one is already there. It backs
up any file it would overwrite. Run it again after a `git pull` to update.

Then add the skills, either as a plugin, from any shell:

```sh
claude plugin marketplace add ali-rafiei/claude-multi-account
claude plugin install claude-multi-account-macos@claude-multi-account
```

(or the same two commands as `/plugin marketplace add ...` and `/plugin install ...` inside
`claude` in a terminal; the VS Code extension's chat answers `/plugin` with "isn't available
in this environment"), or by linking them from the clone with `./install.sh --skills`. Pick
one: with both, each skill shows up twice under two names. As a plugin their full names are
`claude-multi-account-macos:cc-usage` and so on; linked, they are plain `cc-usage`. Either way
you can just ask in words.

Install the plugin on your default login (a plain terminal, or plain `claude`), not inside a
`cc <profile>` session: each time `cc` starts a profile it copies the default login's plugin
list, when it has one, over the profile's own.

To keep profiles somewhere other than `~/.claude-profiles`, run
`CLAUDE_PROFILES=/path/to/dir ./install.sh`; the line it adds to `~/.zshrc` exports that
path for you.

## Quick start

Open a new terminal after installing, then:

```sh
cc-add work          # a browser window opens; sign in with the work account
cc work              # Claude Code, as work
ccusage-all          # usage for your default login and every profile
cc-use work          # VS Code and plain `claude` now run as work (macOS)
cc-use default       # and back
```

## How it works

**Profiles.** A profile is a directory under `~/.claude-profiles/`, which `cc` passes to
Claude Code as `CLAUDE_CONFIG_DIR`. Each profile gets its own login, settings, history and
transcripts. Your default login stays where Claude Code keeps it (the Keychain and
`~/.claude.json`) and never gets a `CLAUDE_CONFIG_DIR`.

**Shared skills and plugins.** Each time `cc` starts a profile, it links `~/.claude/skills`
and `~/.claude/plugins` into it. It also copies `enabledPlugins` and
`extraKnownMarketplaces` from your `~/.claude/settings.json`, because Claude Code records
whether a plugin is on in each account's settings, not in the plugins folder. Install a
skill or plugin once and every profile has it. A profile's other settings stay its own.

**Swapping the default account (`cc-use`).** VS Code and plain `claude` use the default login:
the `Claude Code-credentials` item in the macOS Keychain, plus the account details in
`~/.claude.json`. `cc-use work` swaps in work's login and account details. New sessions
start on it straight away, and running ones pick it up once Claude Code's Keychain cache
expires, which is about 30 seconds. In VS Code, reopening the Claude tab switches at once.
Your own login waits in a separate Keychain item until `cc-use default` puts it back.

Only one live copy of a login exists at a time. Claude Code refreshes tokens as it goes,
and a refresh can leave an older copy dead, so `cc-use` moves a login around instead of
duplicating it: before loading the next account, it writes whatever is in the default slot
back to its owner. A few checks guard the swap:

- It takes the same lock directories Claude Code uses for token refreshes and config
  writes, so a session refreshing in the middle of a swap can't write the old account back.
- It asks Anthropic's profile endpoint whose token is in the slot, and refuses if that isn't
  the account it last put there. If that check fails, look at what's in the slot before
  forcing anything.
- It refuses a profile that has a Claude Code session open, since that session holds its
  own live copy of the login.
- While a profile is loaded, `cc <that profile>` refuses to start, and `ccusage-all` reads
  that account's usage through the default login rather than its now-stale copy.

## Things to know

- `cc` shares its name with the C compiler (`/usr/bin/cc`). The function only takes over in
  your interactive zsh; `make` and build scripts run the compiler as before. Type
  `command cc` when you want the compiler by hand.
- `cc-use` switches every session on the default login at once: all VS Code tabs and every
  plain `claude` in a terminal. Sessions started with `cc <profile>` are unaffected.
- `cc-run` hands tasks over as `claude -p --permission-mode auto`, so the other account can
  edit files and run commands without prompts, with auto mode's classifier still screening
  each action. Haiku has no auto mode, so on Haiku every edit is denied. If the session
  launching it is itself in auto mode, it may refuse to start a second agent; approve it
  from manual mode, or add a Bash allow rule for the launch command.
- This depends on Claude Code internals that aren't a public API: Keychain item names, lock
  directory names, and the `sessions/` folder in a config dir. A Claude Code update can
  change them. Tested against Claude Code 2.1.231.
- Use it only with accounts you're entitled to use, and within Anthropic's terms. It picks
  which of your own accounts does the work; it doesn't get around any limit on a single
  account.

## Uninstall

```sh
cc-use default            # if a profile is loaded; uninstall refuses otherwise
./install.sh --uninstall  # from the clone's macos folder
```

That removes the scripts, the `~/.zshrc` line and any skill links from `--skills`. It also
deletes the spare copy of your login that `cc-use` kept in the Keychain, once it can confirm
your own login is back in the default slot; if it can't (offline, say), it keeps the copy,
which is harmless, and prints the command to delete it later. With a custom
`CLAUDE_PROFILES`, run the uninstall from a terminal that has it set (any new terminal does,
until the uninstall removes the line). If you installed the skills as a plugin, remove it
too, from any shell (or as `/plugin uninstall ...` and `/plugin marketplace remove ...`
inside `claude` in a terminal):

```sh
claude plugin uninstall claude-multi-account-macos@claude-multi-account
claude plugin marketplace remove claude-multi-account
```

Your profiles, their logins and their history stay in `~/.claude-profiles`. Delete a
profile's directory to remove it for good.

## Credits

The lock protocol and the Keychain handling in `cc_use.py` are adapted from
[claude-swap](https://github.com/realiti4/claude-swap) by Onur Cetinkol (MIT); see
[THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md). claude-swap is a fuller tool with
automatic rotation and a dashboard; this repository keeps each account in its own profile
and adds the skills.

## License

MIT; see [LICENSE](../LICENSE). The Windows version lives in [../windows](../windows).
