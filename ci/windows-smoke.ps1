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
    # -EncodedCommand carries quotes intact. Some commands are meant to fail and print to
    # stderr; that must not stop this script.
    $ErrorActionPreference = 'Continue'
    if (-not $UserProfile) { $command = ". '$($profiles -replace "'", "''")\profiles.ps1'; $command" }
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
    $flags = @('-NoLogo', '-ExecutionPolicy', 'Bypass')
    if (-not $UserProfile) { $flags += '-NoProfile' }
    $out = & $Shell @flags -EncodedCommand $encoded 2>&1 | Out-String
    return $out.Trim()
}

function Invoke-Bash([string]$command) {
    $ErrorActionPreference = 'Continue'
    $out = & $bash -c $command 2>&1 | Out-String
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
Set-Content (Join-Path $HOME '.claude.json') '{"oauthAccount": {"accountUuid": "uuid-home", "emailAddress": "me@example.com"}}'
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
$seen = Invoke-Shell 'cc work mcp add x -- npx y' | ConvertFrom-Json
Assert (($seen.args -join ' ') -eq 'mcp add x -- npx y') "cc passes -- through ($Shell): $($seen.args -join ' ')"
$out = Invoke-Shell "`$env:STUB_EXIT = '7'; cc work | Out-Null; `"exit=`$LASTEXITCODE`""
Assert ($out -eq 'exit=7') "cc returns claude's exit code ($Shell): $out"

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

$seen = Invoke-Bash 'source ~/.bashrc; cc work -p from-bash' | ConvertFrom-Json
Assert ($seen.config -eq $work) 'Git Bash: cc work runs claude with the profile config dir'
$seen = Invoke-Bash 'source ~/.bashrc; cc work -p /review' | ConvertFrom-Json
Assert ($seen.args[1] -eq '/review') "Git Bash: cc passes a slash command through: $($seen.args -join ' ')"

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
