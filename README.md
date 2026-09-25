# cc-accounts

Run Claude Code as several accounts on one machine. Each account gets its own profile, one
command shows usage across all of them, Claude can hand a task to another account, and you
can switch which account VS Code and plain `claude`
in a terminal run as, without reloading anything.

Written for people who have more than one Claude subscription or seat (personal and work,
or several team seats) and want to use whichever one has room left this week.

```text
ACCOUNT             PROFILE           SESSION  WEEK  OTHER LIMITS  WEEK RESETS
------------------  ----------------  -------  ----  ------------  ---------------
work@example.com    work [default] *  12%      31%   Fable 18%     Saturday at 3am
me@example.com      personal          0%       64%   Fable 70%     Tuesday at 1pm
team-2@example.com  team-2            0%       2%    Fable 0%      Thursday at 7pm
```

## Pick your version

The two versions are separate, each with its own scripts, installer, skills and tests, so a
change to one never touches the other.

| | Version | Shells | Install |
|---|---|---|---|
| macOS | [macos/](macos) | zsh | `cd macos && ./install.sh` |
| Windows | [windows/](windows) | PowerShell 7, Windows PowerShell 5.1, Git Bash | `cd windows; .\install.ps1` |

Both give you the same commands (`cc`, `cc-add`, `cc-login`, `ccusage-all`, `cc-use`) and
the same three skills (`cc-usage`, `cc-run`, `cc-use`). The skills also install as a Claude
Code plugin. They call the scripts the installer puts in `~/.claude-profiles`, so run the
installer first, then add this repository as a marketplace and install the plugin for your
OS. From any shell:

```sh
claude plugin marketplace add ali-rafiei/cc-accounts
claude plugin install cc-mac@cc-accounts   # macOS
claude plugin install cc-win@cc-accounts   # Windows
```

Run one of the two install lines, not both. Inside `claude` in a terminal, the same thing is
`/plugin marketplace add ali-rafiei/cc-accounts` followed by
`/plugin install cc-<mac|win>@cc-accounts`. Use a terminal for
`/plugin`: the VS Code extension's chat answers it with "/plugin isn't available in this
environment".

Install the plugin or link the skills with the installer (`./install.sh --skills`,
`.\install.ps1 -Skills`), not both: with both, each skill shows up twice under two names.
Each version's README covers requirements, how the account swap works, and uninstalling.

## Credits

The lock protocol and the macOS Keychain handling are adapted from
[claude-swap](https://github.com/realiti4/claude-swap) by Onur Cetinkol (MIT); see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## License

MIT
