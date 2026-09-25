# End-to-end check of the Windows version on a disposable CI machine. It installs for real
# into the runner's profile, so never run it on your own computer.
#
#   pwsh -File ci/windows-smoke.ps1 -Shell pwsh         (or -Shell powershell for 5.1)
param([Parameter(Mandatory)][ValidateSet('pwsh', 'powershell')][string]$Shell)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$profiles = Join-Path $HOME '.claude-profiles'

function Assert([bool]$condition, [string]$what) {
    if (-not $condition) { throw "FAILED: $what" }
    Write-Output "ok: $what"
}

function Invoke-Shell([string]$command) {
    # A fresh shell that loads the commands the way a user's profile would.
    # Some commands are meant to fail and print to stderr; that must not stop this script.
    $ErrorActionPreference = 'Continue'
    $load = ". '$profiles\profiles.ps1'"
    $out = & $Shell -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$load; $command" 2>&1 | Out-String
    return $out.Trim()
}

# A stand-in for Claude Code that reports which config dir and arguments it was given.
$bin = Join-Path $env:RUNNER_TEMP 'stub-bin'
New-Item -ItemType Directory -Force -Path $bin | Out-Null
Set-Content -Path (Join-Path $bin 'claude_stub.py') -Value @'
import json, os, sys
print(json.dumps({'config': os.environ.get('CLAUDE_CONFIG_DIR'), 'args': sys.argv[1:]}))
'@
$python = (Get-Command python).Source
Set-Content -Path (Join-Path $bin 'claude.cmd') -Value "@`"$python`" `"%~dp0claude_stub.py`" %*"
$env:PATH = "$bin;$env:PATH"

# A default login and one logged-in profile, as files, the way Claude Code stores them on Windows.
New-Item -ItemType Directory -Force -Path (Join-Path $HOME '.claude') | Out-Null
Set-Content (Join-Path $HOME '.claude\.credentials.json') '{"claudeAiOauth": {"accessToken": "home-at", "refreshToken": "home-rt"}}'
Set-Content (Join-Path $HOME '.claude.json') '{"oauthAccount": {"accountUuid": "uuid-home", "emailAddress": "me@example.com"}}'
Set-Content (Join-Path $HOME '.claude\settings.json') '{"enabledPlugins": {"tool@market": true}}'

& (Join-Path $repo 'windows\install.ps1') -Skills
$documents = [Environment]::GetFolderPath('MyDocuments')
foreach ($path in @("$documents\WindowsPowerShell\profile.ps1", "$documents\PowerShell\profile.ps1", "$HOME\.bashrc")) {
    Assert ((Get-Content $path -Raw).Contains('# claude-multi-account')) "install added the line to $path"
}
& (Join-Path $repo 'windows\install.ps1')
Assert (((Get-Content "$HOME\.bashrc" -Raw) -split '# claude-multi-account').Count -eq 2) 'a second install adds no second line'

$work = Join-Path $profiles 'work'
New-Item -ItemType Directory -Force -Path $work | Out-Null
Set-Content (Join-Path $work '.credentials.json') '{"claudeAiOauth": {"accessToken": "work-at", "refreshToken": "work-rt"}}'
Set-Content (Join-Path $work '.claude.json') '{"oauthAccount": {"accountUuid": "uuid-work", "emailAddress": "work@example.com"}}'

$seen = Invoke-Shell 'cc work -p hi' | ConvertFrom-Json
Assert ($seen.config -eq $work) "cc work runs claude with the profile's config dir ($Shell)"
Assert (($seen.args -join ' ') -eq '-p hi') "cc work passes the arguments through ($Shell)"
Assert ((Get-Item -Force (Join-Path $work 'skills')).LinkType -eq 'Junction') 'the profile gets a skills junction'
Assert ((Get-Content (Join-Path $work 'settings.json') -Raw).Contains('tool@market')) 'the plugin settings are mirrored'

$seen = Invoke-Shell 'cc default --version' | ConvertFrom-Json
Assert ($null -eq $seen.config) "cc default runs with no config dir ($Shell)"

# The access tokens are fake, so the identity check cannot resolve them and falls back to
# comparing refresh tokens, which is the offline path.
$out = Invoke-Shell 'cc-use work'
Assert ($out.Contains('default -> work')) "cc-use work loads the profile: $out"
Assert ((Get-Content (Join-Path $HOME '.claude\.credentials.json') -Raw).Contains('work-rt')) 'the default slot now holds work'
$out = Invoke-Shell 'cc work'
Assert ($out.Contains('loaded into the default login')) "cc refuses the loaded profile: $out"
$out = Invoke-Shell 'cc-use default'
Assert ($out.Contains('work -> default')) "cc-use default restores the user's login: $out"
Assert ((Get-Content (Join-Path $HOME '.claude\.credentials.json') -Raw).Contains('home-rt')) 'the default slot holds the user again'

$bash = Join-Path $env:ProgramFiles 'Git\bin\bash.exe'
$seen = & $bash -c 'source ~/.bashrc; cc work -p from-bash' | ConvertFrom-Json
Assert ($seen.config -eq $work) 'Git Bash: cc work runs claude with the profile config dir'

& (Join-Path $repo 'windows\install.ps1') -Uninstall
Assert (-not (Test-Path (Join-Path $profiles 'cc_use.py'))) 'uninstall removes the scripts'
Assert (-not (Test-Path (Join-Path $profiles '.home-credentials.json'))) 'uninstall deletes the stashed login'
Assert (-not ((Get-Content "$HOME\.bashrc" -Raw) -match 'claude-multi-account')) 'uninstall removes the bash line'
Assert (-not (Test-Path (Join-Path $HOME '.claude\skills\cc-use'))) 'uninstall removes the skill junctions'
Assert (Test-Path (Join-Path $repo 'windows\skills\cc-use\SKILL.md')) 'removing a skill junction leaves the repo copy'
Assert (Test-Path $work) 'uninstall keeps the profile'
Write-Output "windows smoke test passed ($Shell)"
