# End-to-end check of the Windows version on a disposable CI machine. It installs for real
# into the runner's profile, so never run it on your own computer.
#
#   pwsh -File ci/windows-smoke.ps1 -Shell pwsh         (or -Shell powershell for 5.1)
param([Parameter(Mandatory)][ValidateSet('pwsh', 'powershell')][string]$Shell)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$profiles = Join-Path $HOME '.claude-profiles'
$installer = Join-Path $repo 'windows\install.ps1'
$bash = Join-Path $env:ProgramFiles 'Git\bin\bash.exe'
$documents = [Environment]::GetFolderPath('MyDocuments')
$ps5Profile = Join-Path $documents 'WindowsPowerShell\profile.ps1'
$ps7Profile = Join-Path $documents 'PowerShell\profile.ps1'
$bashrc = Join-Path $HOME '.bashrc'

$failures = New-Object System.Collections.Generic.List[string]

# Records a failure and carries on, so one run reports every broken check.
function Assert([bool]$condition, [string]$what) {
    if ($condition) { Write-Output "ok: $what" } else { $failures.Add($what); Write-Output "FAILED: $what" }
}

function Invoke-Shell([string]$command, [switch]$UserProfile) {
    # A fresh shell that loads the commands the way a user's profile would: by dot-sourcing
    # profiles.ps1, or with -UserProfile through the line the installer put in the profile.
    # A script file carries quotes intact (-EncodedCommand would make the child's stderr
    # CLIXML). Some commands are meant to fail and print to stderr; that must not stop this.
    $ErrorActionPreference = 'Continue'
    if (-not $UserProfile) { $command = ". '$($profiles -replace "'", "''")\profiles.ps1'; $command" }
    $script = Join-Path $env:RUNNER_TEMP 'smoke-command.ps1'
    [IO.File]::WriteAllText($script, $command, (New-Object Text.UTF8Encoding $true))
    $flags = @('-NoLogo', '-ExecutionPolicy', 'Bypass')
    if (-not $UserProfile) { $flags += '-NoProfile' }
    $out = & $Shell @flags -File $script 2>&1 | Out-String
    return $out.Trim()
}

function Invoke-Bash([string]$command, [switch]$Login) {
    # -Login starts bash the way the Git Bash shortcut does, reading ~/.bash_profile and not ~/.bashrc.
    $ErrorActionPreference = 'Continue'
    $flags = @('-c')
    if ($Login) { $flags = @('--login', '-c') }
    $out = & $bash @flags $command 2>&1 | Out-String
    return $out.Trim()
}

function Invoke-Installer([string[]]$arguments) {
    # The way the README runs it, in the shell under test.
    $ErrorActionPreference = 'Continue'
    $out = & $Shell -NoLogo -NoProfile -ExecutionPolicy Bypass -File $installer @arguments *>&1 | Out-String
    if ($LASTEXITCODE -ne 0) { throw "install.ps1 $arguments failed: $out" }
    return $out
}

function Get-Bom([string]$path) {
    $bytes = [IO.File]::ReadAllBytes($path)
    return (($bytes | Select-Object -First 3) | ForEach-Object { $_.ToString('X2') }) -join ''
}

# A stand-in for Claude Code that reports which config dir and arguments it was given.
$bin = Join-Path $env:RUNNER_TEMP 'stub-bin'
New-Item -ItemType Directory -Force -Path $bin | Out-Null
Set-Content -Path (Join-Path $bin 'claude_stub.py') -Value @'
import json, os, sys
print(json.dumps({'config': os.environ.get('CLAUDE_CONFIG_DIR'), 'args': sys.argv[1:]}))
sys.exit(int(os.environ.get('STUB_EXIT', '0')))
'@
$python = (Get-Command python).Source
Set-Content -Path (Join-Path $bin 'claude.cmd') -Value "@`"$python`" `"%~dp0claude_stub.py`" %*"
$env:PATH = "$bin;$env:PATH"

# A default login and one logged-in profile, as files, the way Claude Code stores them on Windows.
New-Item -ItemType Directory -Force -Path (Join-Path $HOME '.claude') | Out-Null
Set-Content (Join-Path $HOME '.claude\.credentials.json') '{"claudeAiOauth": {"accessToken": "home-at", "refreshToken": "home-rt"}}'
# Claude Code writes UTF-8, and a non-ASCII name holds bytes Windows' cp1252 cannot decode (0x81, in L-stroke).
[IO.File]::WriteAllText((Join-Path $HOME '.claude.json'), "{`"oauthAccount`": {`"accountUuid`": `"uuid-home`", `"emailAddress`": `"me@example.com`", `"displayName`": `"$([char]0x141)ukasz`"}}", (New-Object Text.UTF8Encoding $false))
Set-Content (Join-Path $HOME '.claude\settings.json') '{"enabledPlugins": {"tool@market": true}}'

# Existing files in the encodings users really have: Windows PowerShell 5.1's `>` writes
# UTF-16 LE, Set-Content -Encoding UTF8 writes a BOM, and a .bashrc has LF line endings.
New-Item -ItemType Directory -Force -Path (Split-Path $ps5Profile), (Split-Path $ps7Profile) | Out-Null
[IO.File]::WriteAllText($ps5Profile, "# keep caf$([char]0xE9)`r`n", [Text.Encoding]::Unicode)
[IO.File]::WriteAllText($ps7Profile, "# keep caf$([char]0xE9)`r`n", (New-Object Text.UTF8Encoding $true))
[IO.File]::WriteAllText($bashrc, "export KEEP=1`n")

Invoke-Installer @('-Skills') | Out-Null
foreach ($path in @($ps5Profile, $ps7Profile, $bashrc)) {
    Assert ([IO.File]::ReadAllText($path).Contains('# claude-multi-account')) "install added the line to $path"
}
foreach ($path in @($ps5Profile, $ps7Profile)) {
    Assert ([IO.File]::ReadAllText($path).Contains("caf$([char]0xE9)")) "install kept the existing text of $path"
}
Assert (-not ([IO.File]::ReadAllBytes($bashrc) -contains 13)) 'install writes LF line endings into .bashrc'
$out = Invoke-Bash 'source ~/.bashrc'
Assert ($out -eq '') "Git Bash sources the installed .bashrc cleanly: $out"
Invoke-Installer @() | Out-Null
Assert (((Get-Content $bashrc -Raw) -split '# claude-multi-account').Count -eq 2) 'a second install adds no second line'

$work = Join-Path $profiles 'work'
New-Item -ItemType Directory -Force -Path $work | Out-Null
Set-Content (Join-Path $work '.credentials.json') '{"claudeAiOauth": {"accessToken": "work-at", "refreshToken": "work-rt"}}'
Set-Content (Join-Path $work '.claude.json') '{"oauthAccount": {"accountUuid": "uuid-work", "emailAddress": "work@example.com"}}'

$seen = Invoke-Shell 'cc work -p hi' | ConvertFrom-Json
Assert ($seen.config -eq $work) "cc work runs claude with the profile's config dir ($Shell)"
Assert (($seen.args -join ' ') -eq '-p hi') "cc work passes the arguments through ($Shell)"
Assert ((Get-Item -Force (Join-Path $work 'skills')).LinkType -eq 'Junction') 'the profile gets a skills junction'
Assert ((Get-Content (Join-Path $work 'settings.json') -Raw).Contains('tool@market')) 'the plugin settings are mirrored'

$seen = Invoke-Shell "cc work -p 'say `"hi there`"' ''" | ConvertFrom-Json
Assert ($seen.args.Count -eq 3 -and $seen.args[1] -eq 'say "hi there"' -and $seen.args[2] -eq '') "cc keeps embedded quotes and empty arguments ($Shell): $($seen.args -join '|')"
$out = Invoke-Shell "`$env:STUB_EXIT = '7'; cc work | Out-Null; `"exit=`$LASTEXITCODE`""
Assert ($out -eq 'exit=7') "cc returns claude's exit code ($Shell): $out"

$seen = Invoke-Shell 'cc default --version' | ConvertFrom-Json
Assert ($null -eq $seen.config) "cc default runs with no config dir ($Shell)"

$out = Invoke-Shell 'ccusage-all'
Assert ($out.Contains('me@example.com') -and $out.Contains('work@example.com') -and -not $out.Contains('Traceback')) "ccusage-all lists every account ($Shell): $out"

# The access tokens are fake, so the identity check cannot resolve them and falls back to
# comparing refresh tokens, which is the offline path.
$out = Invoke-Shell 'cc-use work'
Assert ($out.Contains('default -> work')) "cc-use work loads the profile: $out"
Assert ((Get-Content (Join-Path $HOME '.claude\.credentials.json') -Raw).Contains('work-rt')) 'the default slot now holds work'
$out = Invoke-Shell 'cc work'
Assert ($out.Contains('loaded into the default login')) "cc refuses the loaded profile: $out"
$out = Invoke-Shell 'ccusage-all'
Assert ($out.Contains('work [default]') -and -not $out.Contains('Traceback')) "ccusage-all marks the loaded profile ($Shell): $out"
$out = Invoke-Shell 'cc-use default'
Assert ($out.Contains('work -> default')) "cc-use default restores the user's login: $out"
Assert ((Get-Content (Join-Path $HOME '.claude\.credentials.json') -Raw).Contains('home-rt')) 'the default slot holds the user again'

$seen = Invoke-Bash 'source ~/.bashrc; cc work -p from-bash' | ConvertFrom-Json
Assert ($seen.config -eq $work) 'Git Bash: cc work runs claude with the profile config dir'
$seen = Invoke-Bash 'source ~/.bashrc; export CLAUDE_PROFILES="$HOME/.claude-profiles"; cc work -p posix' | ConvertFrom-Json
Assert ($seen.config -eq $work) "Git Bash: a POSIX-style CLAUDE_PROFILES reaches Python as a Windows path: $($seen.config)"

Invoke-Installer @('-Uninstall') | Out-Null
Assert (-not (Test-Path (Join-Path $profiles 'cc_use.py'))) 'uninstall removes the scripts'
Assert (-not (Test-Path (Join-Path $profiles '.home-credentials.json'))) 'uninstall deletes the stashed login'
Assert (-not ((Get-Content $bashrc -Raw) -match 'claude-multi-account')) 'uninstall removes the bash line'
Assert ([IO.File]::ReadAllText($bashrc) -eq "export KEEP=1`n") "uninstall leaves .bashrc as it was: $([IO.File]::ReadAllText($bashrc) -replace "`r", '\r')"
Assert ((Get-Bom $ps5Profile) -like 'FFFE*') 'uninstall keeps the UTF-16 profile UTF-16'
Assert ((Get-Bom $ps7Profile) -eq 'EFBBBF') 'uninstall keeps the UTF-8 BOM profile BOM'
foreach ($path in @($ps5Profile, $ps7Profile)) {
    $text = [IO.File]::ReadAllText($path)
    Assert ($text.Contains("caf$([char]0xE9)") -and -not $text.Contains('claude-multi-account')) "uninstall removes only our line from $path"
}
Assert (-not (Test-Path (Join-Path $HOME '.claude\skills\cc-use'))) 'uninstall removes the skill junctions'
Assert (Test-Path (Join-Path $repo 'windows\skills\cc-use\SKILL.md')) 'removing a skill junction leaves the repo copy'
Assert (Test-Path $work) 'uninstall keeps the profile'

# Uninstall goes on when cc-use refuses to delete its stash, and says how to delete it later.
Invoke-Installer @() | Out-Null
$stash = Join-Path $profiles '.home-credentials.json'
Set-Content $stash '{"claudeAiOauth": {"refreshToken": "home-rt"}}'
Set-Content (Join-Path $profiles 'cc_use.py') "import sys`nprint('cc-use: not deleting it', file=sys.stderr)`nsys.exit(1)"
$out = Invoke-Installer @('-Uninstall')
Assert ($out.Contains('kept') -and $out.Contains('.home-credentials.json')) "uninstall reports the kept stash and how to delete it: $out"
Assert (Test-Path $stash) 'uninstall leaves the stash when cc-use refuses'
Assert (-not ([IO.File]::ReadAllText($ps7Profile).Contains('claude-multi-account'))) 'uninstall still removes the line when cc-use refuses'
Remove-Item $stash

# A custom profiles folder whose path has a space and an apostrophe, as under C:\Users\O'Brien.
$custom = Join-Path $env:RUNNER_TEMP "O'Brien profiles"
New-Item -ItemType Directory -Force -Path (Join-Path $custom 'work') | Out-Null
$env:CLAUDE_PROFILES = $custom
Invoke-Installer @() | Out-Null
Remove-Item Env:CLAUDE_PROFILES
$out = Invoke-Shell 'cc' -UserProfile
Assert ($out.Contains('profiles: default work')) "the profile line loads a custom folder with an apostrophe ($Shell): $out"
$out = Invoke-Bash 'source ~/.bashrc; cc'
Assert ($out.Contains('profiles: default work')) "Git Bash: the .bashrc line loads a custom folder with an apostrophe: $out"
$env:CLAUDE_PROFILES = $custom
Invoke-Installer @('-Uninstall') | Out-Null
Remove-Item Env:CLAUDE_PROFILES
Assert (-not ([IO.File]::ReadAllText($ps7Profile).Contains('claude-multi-account'))) 'uninstall from a custom folder removes the line'

# A relative CLAUDE_PROFILES is anchored where the installer ran, not wherever a new shell opens.
$installCwd = Join-Path $env:RUNNER_TEMP 'install-cwd'
New-Item -ItemType Directory -Force -Path (Join-Path $installCwd 'rel-profiles\work') | Out-Null
Push-Location $installCwd
try { $env:CLAUDE_PROFILES = 'rel-profiles'; Invoke-Installer @() | Out-Null } finally { Remove-Item Env:CLAUDE_PROFILES; Pop-Location }
$out = Invoke-Shell 'cc' -UserProfile
Assert ($out.Contains('profiles: default work')) "the profile line loads a relative install location from elsewhere ($Shell): $out"
$out = Invoke-Bash 'source ~/.bashrc; cc'
Assert ($out.Contains('profiles: default work')) "Git Bash: the .bashrc line loads a relative install location from elsewhere: $out"
$env:CLAUDE_PROFILES = Join-Path $installCwd 'rel-profiles'
Invoke-Installer @('-Uninstall') | Out-Null
Remove-Item Env:CLAUDE_PROFILES
Assert (-not ([IO.File]::ReadAllText($ps7Profile).Contains('claude-multi-account'))) 'uninstall from a relative install location removes the line'

$env:CLAUDE_PROFILES = '~\tilde-profiles'
try { Invoke-Installer @() | Out-Null } finally { Remove-Item Env:CLAUDE_PROFILES }
$out = Invoke-Bash 'source ~/.bashrc; cc'
Assert ($out.Contains('profiles: default') -and -not $out.Contains('No such file')) "Git Bash: the .bashrc line finds a location given with a leading ~: $out"
$env:CLAUDE_PROFILES = Join-Path $HOME 'tilde-profiles'
Invoke-Installer @('-Uninstall') | Out-Null
Remove-Item Env:CLAUDE_PROFILES

# Git Bash starts as a login shell, which reads an existing ~/.bash_profile instead of ~/.bashrc.
$bashProfile = Join-Path $HOME '.bash_profile'
[IO.File]::WriteAllText($bashProfile, "export KEEP_PROFILE=1`n")
Invoke-Installer @() | Out-Null
$out = Invoke-Bash 'cc' -Login
Assert ($out.Contains('profiles: default work')) "Git Bash as a login shell loads the commands past a .bash_profile that skips .bashrc: $out"
Invoke-Installer @('-Uninstall') | Out-Null
Assert ([IO.File]::ReadAllText($bashProfile) -eq "export KEEP_PROFILE=1`n") "uninstall leaves .bash_profile as it was: $([IO.File]::ReadAllText($bashProfile))"
$sourcesBashrc = "test -f ~/.bashrc && . ~/.bashrc`n"
[IO.File]::WriteAllText($bashProfile, $sourcesBashrc)
Invoke-Installer @() | Out-Null
Assert ([IO.File]::ReadAllText($bashProfile) -eq $sourcesBashrc) 'install leaves alone a .bash_profile that sources .bashrc'
$out = Invoke-Bash 'cc' -Login
Assert ($out.Contains('profiles: default work')) "Git Bash as a login shell loads the commands through .bashrc: $out"
Invoke-Installer @('-Uninstall') | Out-Null
Remove-Item $bashProfile

# The installer runs under -ExecutionPolicy Bypass, but the policy a new shell loads the
# profile under is the user's own.
& $Shell -NoLogo -NoProfile -Command 'Set-ExecutionPolicy -Scope CurrentUser Restricted -Force' 2>&1 | Out-Null
try {
    $out = Invoke-Installer @()
    Assert ($out.Contains('execution policy is Restricted')) "install warns that a Restricted policy blocks the profile ($Shell): $out"
} finally {
    & $Shell -NoLogo -NoProfile -Command 'Set-ExecutionPolicy -Scope CurrentUser Undefined -Force' 2>&1 | Out-Null
    Invoke-Installer @('-Uninstall') | Out-Null
}
if ($failures.Count) { throw "$($failures.Count) check(s) failed ($Shell):`n$($failures -join "`n")" }
Write-Output "windows smoke test passed ($Shell)"
