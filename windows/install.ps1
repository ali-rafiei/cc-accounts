<#
Install the Windows version of claude-multi-account.

  .\install.ps1              copy the scripts into $env:CLAUDE_PROFILES (default ~\.claude-profiles)
                             and load the commands from your PowerShell profile and ~\.bashrc
  .\install.ps1 -Skills      also link the skills into ~\.claude\skills (skip if you use the plugin)
  .\install.ps1 -Uninstall   undo both and delete cc-use's stashed login; profiles are left alone

A file already installed that differs from this copy is backed up beside itself first.
If scripts are blocked, run it as: powershell -ExecutionPolicy Bypass -File .\install.ps1
#>
[CmdletBinding()]
param([switch]$Skills, [switch]$Uninstall)

$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot
$dest = if ($env:CLAUDE_PROFILES) { $env:CLAUDE_PROFILES.TrimEnd('\', '/') } else { Join-Path $HOME '.claude-profiles' }
$defaultDest = Join-Path $HOME '.claude-profiles'
$scripts = @('profiles.ps1', 'profiles.sh', 'cc_run.py', 'cc_use.py', 'usage_table.py')
$skillNames = @('cc-usage', 'cc-use', 'cc-run')
$skillsDir = Join-Path $HOME '.claude\skills'
$marker = '# claude-multi-account'
# Windows PowerShell 5.1 and PowerShell 7 each read their own profile, so both get the line.
$documents = [Environment]::GetFolderPath('MyDocuments')
$psProfiles = @((Join-Path $documents 'WindowsPowerShell\profile.ps1'), (Join-Path $documents 'PowerShell\profile.ps1'))
$bashrc = Join-Path $HOME '.bashrc'

function Main {
    if ($Uninstall) { Uninstall-Scripts; return }
    Install-Scripts
    if ($Skills) { Install-SkillLinks }
}

function Install-Scripts {
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    foreach ($name in $scripts) { Copy-WithBackup (Join-Path $repo "scripts\$name") (Join-Path $dest $name) }

    if ($dest -eq $defaultDest) {
        $psLine = ". `"`$HOME\.claude-profiles\profiles.ps1`"  $marker"
        $bashLine = "source `"`$HOME/.claude-profiles/profiles.sh`"  $marker"
    } else {
        $bashDest = $dest -replace '\\', '/'
        $psLine = "`$env:CLAUDE_PROFILES = '$dest'; . '$dest\profiles.ps1'  $marker"
        $bashLine = "export CLAUDE_PROFILES='$bashDest'; source '$bashDest/profiles.sh'  $marker"
    }
    foreach ($path in $psProfiles) { Add-LineOnce $path $psLine 'profiles.ps1' }
    Add-LineOnce $bashrc $bashLine 'profiles.sh'

    $policy = Get-ExecutionPolicy
    if ($policy -in @('Restricted', 'AllSigned')) {
        Write-Warning "PowerShell's execution policy is $policy, so your profile will not load these commands. Allow local scripts with: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned"
    }
    if (-not ((Get-Command py -ErrorAction SilentlyContinue) -or (Get-Command python -ErrorAction SilentlyContinue))) {
        Write-Warning 'Python was not found. Install it from python.org (tick "Add to PATH") before using the commands.'
    }
    Write-Output @"

Installed into $dest. Open a new PowerShell or Git Bash window, then:
  cc-add <profile>     create an account profile and log it in
  cc <profile>         run Claude Code as it
  ccusage-all          usage across every account
  cc-use <profile>     make VS Code and plain ``claude`` run as it; cc-use default undoes
"@
}

function Uninstall-Scripts {
    $loadedFile = Join-Path $dest '.loaded'
    if (Test-Path $loadedFile) {
        $loaded = (Get-Content $loadedFile -Raw).Trim()
        Write-Error "cc-use has '$loaded' in your default login. Run ``cc-use default`` first."
    }
    if (Test-Path (Join-Path $dest '.home-account.json')) {
        Invoke-Python (Join-Path $dest 'cc_use.py') forget
    }
    foreach ($name in $scripts) { Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $dest $name) }
    foreach ($path in $psProfiles) { Remove-MarkedLines $path }
    Remove-MarkedLines $bashrc
    foreach ($name in $skillNames) {
        $link = Join-Path $skillsDir $name
        $item = Get-Item -Force -ErrorAction SilentlyContinue $link
        if ($item -and $item.LinkType -eq 'Junction' -and ($item.Target -join '') -eq (Join-Path $repo "skills\$name")) {
            # Deletes the junction itself; never recurse into it, or its target's files go too.
            [System.IO.Directory]::Delete($link)
        }
    }
    Write-Output "Uninstalled. Profiles in $dest, and the skills/plugins junctions inside them, were left in place."
}

function Install-SkillLinks {
    New-Item -ItemType Directory -Force -Path $skillsDir | Out-Null
    foreach ($name in $skillNames) {
        $link = Join-Path $skillsDir $name
        if (Test-Path $link) {
            Write-Output "skipped skill ${name}: $link already exists"
        } else {
            New-Item -ItemType Junction -Path $link -Target (Join-Path $repo "skills\$name") | Out-Null
            Write-Output "linked skill $name"
        }
    }
}

function Copy-WithBackup([string]$source, [string]$target) {
    if ((Test-Path $target) -and ((Get-FileHash $source).Hash -ne (Get-FileHash $target).Hash)) {
        $backup = "$target.bak-$(Get-Date -Format yyyyMMddHHmmss)"
        Copy-Item $target $backup
        Write-Output "backed up $target -> $backup"
    }
    Copy-Item -Force $source $target
}

# Adds the line unless the file already loads this script, by our marker or a hand-written line.
function Add-LineOnce([string]$path, [string]$line, [string]$scriptName) {
    if (Test-Path $path) {
        $text = Get-Content $path -Raw
        if ($text -and ($text.Contains($marker) -or $text.Contains(".claude-profiles\$scriptName") -or $text.Contains(".claude-profiles/$scriptName"))) {
            return
        }
    } else {
        New-Item -ItemType File -Force -Path $path | Out-Null
    }
    [System.IO.File]::AppendAllText($path, [Environment]::NewLine + $line + [Environment]::NewLine)
    Write-Output "added to ${path}: $line"
}

# Rewrites the file in place, so a symlinked profile stays a symlink.
function Remove-MarkedLines([string]$path) {
    if (-not (Test-Path $path)) { return }
    $lines = [System.IO.File]::ReadAllLines($path)
    $kept = @($lines | Where-Object { -not $_.Contains($marker) })
    if ($kept.Count -eq $lines.Count) { return }
    while ($kept.Count -gt 0 -and $kept[-1] -eq '') { $kept = @($kept | Select-Object -SkipLast 1) }
    [System.IO.File]::WriteAllLines($path, [string[]]$kept)
    Write-Output "removed the claude-multi-account line from $path"
}

function Invoke-Python {
    if (Get-Command py -ErrorAction SilentlyContinue) { & py -3 @args } else { & python @args }
    if ($LASTEXITCODE -ne 0) { throw "python $($args -join ' ') failed" }
}

Main
