# claude-multi-account for Windows

Run Claude Code as several accounts on one Windows machine. Each account gets its own
profile, one command shows usage across all of them, Claude can hand a task to another
account, and you can switch which account VS Code and plain `claude`
in a terminal run as, without reloading anything.

Works in PowerShell 7, Windows PowerShell 5.1 and Git Bash. The macOS version is separate,
in [../macos](../macos).

## What you get

Commands, the same in PowerShell and Git Bash:

| Command | What it does |
|---|---|
| `cc-add <profile>` | Create a profile and log it in |
| `cc <profile> [args]` | Run Claude Code as that account (`cc default` runs your normal login) |
| `cc-login <profile>` | Log a profile in again |
| `ccusage-all` | One table of session and weekly usage for every account, soonest reset first |
| `cc-use <profile>` | Put that account's login in the default slot, so VS Code and plain `claude` run as it. `cc-use default` puts yours back |

Skills, so you can ask Claude directly:

| Skill | Say something like |
|---|---|
| `cc-usage` | "check my Claude usage", "which account has headroom?" |
| `cc-run` | "have work run the test suite", "ask personal, using opus, to review this" |
| `cc-use` | "switch VS Code to work", "switch to whichever account has room", "put my account back" |

## Requirements

- Windows 10 or 11 with Claude Code installed and your normal account logged in
- Python 3.9 or newer from [python.org](https://www.python.org/downloads/), with "Add
  python.exe to PATH" ticked. The commands use the `py` launcher that comes with it, so they
  don't trip over the Microsoft Store's `python` placeholder.
- Git for Windows, which Claude Code needs anyway, if you want the commands in Git Bash

## Install

```powershell
git clone https://github.com/ali-rafiei/claude-multi-account.git
cd claude-multi-account\windows
.\install.ps1
```

If PowerShell refuses to run the script, use
`powershell -ExecutionPolicy Bypass -File .\install.ps1`. The installer warns you if your
execution policy would also stop your PowerShell profile from loading the commands; the
usual fix is `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

`install.ps1` copies five files into `~\.claude-profiles` (or `$env:CLAUDE_PROFILES`). It
then adds one line to your PowerShell profiles (both Windows PowerShell's and PowerShell
7's) and to `~\.bashrc` for Git Bash, unless the line is already there. If you have a
`~\.bash_profile` that doesn't load `~\.bashrc`, it adds the line there too. A relative or
`~` path in `$env:CLAUDE_PROFILES` is turned into a full path first. It backs up any file it
would overwrite. Run it again after a `git pull` to update.

Git Bash starts a login shell, which reads `~/.bash_profile` (or `~/.bash_login` or
`~/.profile`), not `~/.bashrc`. If you have none of those, the first Git Bash window after
installing warns that it found `~/.bashrc` but no `~/.bash_profile` and creates one that
loads it; that is expected. If you already have one that doesn't load `~/.bashrc`, the
installer adds its line to that file as well.

Then add the skills, either as a plugin, from PowerShell or Git Bash:

```powershell
claude plugin marketplace add ali-rafiei/claude-multi-account
claude plugin install claude-multi-account-windows@claude-multi-account
```

(or the same two commands as `/plugin marketplace add ...` and `/plugin install ...` inside
`claude` in a terminal; the VS Code extension's chat answers `/plugin` with "isn't available
in this environment"), or by linking them from the clone with `.\install.ps1 -Skills`. Pick
one: with both, each skill shows up twice under two names. As a plugin their full names are
`claude-multi-account-windows:cc-usage` and so on; linked, they are plain `cc-usage`. Either
way you can just ask in words.

Install the plugin on your default login (a plain terminal, or plain `claude`), not inside a
`cc <profile>` session: each time `cc` starts a profile it copies the default login's plugin
list, when it has one, over the profile's own.

To keep profiles somewhere other than `~\.claude-profiles`, set `$env:CLAUDE_PROFILES` to
an absolute path before running the installer; the lines it adds set that path for you:

```powershell
$env:CLAUDE_PROFILES = 'D:\claude-profiles'
.\install.ps1
```

## Quick start

Open a new PowerShell window after installing, then:

```powershell
cc-add work          # your browser opens; sign in with the work account
cc work              # Claude Code, as work
ccusage-all          # usage for your default login and every profile
cc-use work          # VS Code and plain `claude` now run as work
cc-use default       # and back
```

When you log a profile in, sign in with the account that profile is for. If your browser is
already signed into a different Claude account, copy the sign-in link Claude shows into a
private window instead.

## How it works

**Profiles.** A profile is a folder under `~\.claude-profiles\`, which `cc` passes to Claude
Code as `CLAUDE_CONFIG_DIR`. Each profile gets its own login, settings, history and
transcripts. Your default login stays where Claude Code keeps it and never gets a
`CLAUDE_CONFIG_DIR`.

**Shared skills and plugins.** Each time `cc` starts a profile, it gives the profile
directory junctions to `~\.claude\skills` and `~\.claude\plugins` (junctions need no admin
rights or Developer Mode). It also copies `enabledPlugins` and `extraKnownMarketplaces` from
your `~\.claude\settings.json`, because Claude Code records whether a plugin is on in each
account's settings. Install a skill or plugin once and every profile has it. A profile's
other settings stay its own.

**Swapping the default account (`cc-use`).** On Windows, Claude Code keeps a login as a file:
`~\.claude\.credentials.json` for the default login, `<profile>\.credentials.json` for a
profile. VS Code and plain `claude` use the default one, plus the account details in
`~\.claude.json`. `cc-use work` swaps in work's login and account details. New sessions
start on it straight away, and running ones pick it up on their next message. Your own login
waits in `~\.claude-profiles\.home-credentials.json` until `cc-use default` puts it back and
deletes that spare copy. If a swap is cut off part way (a closed window, say), `cc-use
default` works out which profile's login is in the slot and finishes putting yours back.

Only one live copy of a login exists at a time. Claude Code refreshes tokens as it goes, and
a refresh can leave an older copy dead, so `cc-use` moves a login around instead of
duplicating it: before loading the next account, it writes whatever is in the default slot
back to its owner. A few checks guard the swap:

- It takes the same lock folders Claude Code uses for token refreshes and config writes, so
  a session refreshing in the middle of a swap can't write the old account back.
- It asks Anthropic's profile endpoint whose token is in the slot, and refuses if that isn't
  the account it last put there. If that check fails, look at what's in the slot before
  forcing anything.
- It refuses a profile that has a Claude Code session open, since that session holds its own
  live copy of the login.
- While a profile is loaded, `cc <that profile>` refuses to start, and `ccusage-all` reads that
  account's usage through the default login rather than its now-stale copy.
- If antivirus or a sync tool has a file open for a moment, it retries for about a second
  before giving up.

## Things to know

- A new profile's name (`cc-add`) uses letters, digits, spaces and `. _ -`, starts with a
  letter or digit, and doesn't end in a dot or space. A folder you made before this rule, such
  as `alice@corp` or `josé`, still works, as long as it contains no `\`, `/` or `:`, doesn't
  end in a dot or space, and doesn't start with `.` or `_`. `default` and `bin` are reserved
  in any case.
- PowerShell swallows a bare `--` before `cc` sees it; write `'--'` to pass one through to
  Claude. Git Bash rewrites an argument that looks like a path, so `cc work -p /review` arrives
  as `C:/Program Files/Git/review`; write `//review` instead.
- `cc-use` switches every session on the default login at once: all VS Code tabs and every
  plain `claude`. Sessions started with `cc <profile>` are unaffected.
- Logins on Windows are plain JSON files in your user folder; that is how Claude Code itself
  stores them there. The copies `cc-use` keeps sit in your user folder the same way.
- For interactive `cc <profile>` sessions in Git Bash, run Git Bash inside Windows Terminal.
  The older mintty window may not give Claude Code a proper console. PowerShell has no such
  issue.
- `cc-run` hands tasks over as `claude -p --permission-mode auto`, so the other account can
  edit files and run commands without prompts, with auto mode's classifier still screening
  each action. Haiku has no auto mode, so on Haiku every edit is denied.
- This depends on Claude Code internals that aren't a public API: the credentials file
  location, lock folder names, and the `sessions\` folder in a config dir. A Claude Code
  update can change them.
- Use it only with accounts you're entitled to use, and within Anthropic's terms. It picks
  which of your own accounts does the work; it doesn't get around any limit on a single
  account.

## Uninstall

```powershell
cc-use default              # if a profile is loaded; uninstall refuses otherwise
.\install.ps1 -Uninstall    # from the clone's windows folder
```

That removes the scripts, the lines in your PowerShell profiles and `~\.bashrc`, and any
skill links from `-Skills`. If `cc-use` still holds a copy of your login (normally it
doesn't, since `cc-use default` deletes it), uninstall deletes it once it can confirm your own
login is back in the default slot. If another account is in the slot, uninstall stops before
removing anything, because that copy may be your only login: run `cc-use default` first. If
it just can't check (offline), it keeps the copy and prints the command to delete it later. With a
custom `CLAUDE_PROFILES`, run the uninstall from a PowerShell window that has it set (any new
window does, until the uninstall removes the line). If you installed the skills as a plugin,
remove it too, from PowerShell or Git Bash (or as `/plugin uninstall ...` and
`/plugin marketplace remove ...` inside `claude` in a terminal):

```powershell
claude plugin uninstall claude-multi-account-windows@claude-multi-account
claude plugin marketplace remove claude-multi-account
```

Your profiles, their logins and their history stay in `~\.claude-profiles`. Delete a
profile's folder to remove it for good.

## Credits

The lock protocol in `cc_use.py` is adapted from
[claude-swap](https://github.com/realiti4/claude-swap) by Onur Cetinkol (MIT); see
[THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).

## License

MIT; see [LICENSE](../LICENSE).
