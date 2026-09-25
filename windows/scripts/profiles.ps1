# claude-multi-account for Windows: the PowerShell commands.
#
# Works in Windows PowerShell 5.1 and PowerShell 7. Each command is a thin wrapper; the work
# happens in the Python scripts beside this file, which Git Bash (profiles.sh) calls too.
#
#   cc <profile> [claude args]   run Claude Code as that account (`cc default` runs your own)
#   cc-add <profile>             create a profile and log it in
#   cc-login <profile>           log a profile in again
#   cc-use <profile>             make VS Code and plain `claude` run as it; `cc-use default` undoes
#   ccusage-all                  usage across every account

function Get-CcProfilesDir {
    $dir = if ($env:CLAUDE_PROFILES) { $env:CLAUDE_PROFILES } else { Join-Path $HOME '.claude-profiles' }
    $dir.TrimEnd('\', '/')
}

function Invoke-CcPython {
    # Prefer the py launcher: on a fresh Windows install a bare `python` can be the Store stub.
    if (Get-Command py -ErrorAction SilentlyContinue) { & py -3 @args } else { & python @args }
}

function cc {
    $script = Join-Path (Get-CcProfilesDir) 'cc_run.py'
    if ($args.Count -eq 0) { Invoke-CcPython $script --help } else { Invoke-CcPython $script run @args }
}

function cc-add { Invoke-CcPython (Join-Path (Get-CcProfilesDir) 'cc_run.py') add @args }

function cc-login { Invoke-CcPython (Join-Path (Get-CcProfilesDir) 'cc_run.py') login @args }

function cc-use { Invoke-CcPython (Join-Path (Get-CcProfilesDir) 'cc_use.py') @args }

function ccusage-all { Invoke-CcPython (Join-Path (Get-CcProfilesDir) 'usage_table.py') @args }
