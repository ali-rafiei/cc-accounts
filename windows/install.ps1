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
        # Quoted for each shell, so a path like C:\Users\O'Brien neither breaks nor injects.
        $psDest = $dest -replace "'", "''"
        $bashDest = ($dest -replace '\\', '/') -replace "'", "'\''"
        $psLine = "`$env:CLAUDE_PROFILES = '$psDest'; . '$psDest\profiles.ps1'  $marker"
        $bashLine = "export CLAUDE_PROFILES='$bashDest'; source '$bashDest/profiles.sh'  $marker"
    }
    foreach ($path in $psProfiles) { Add-LineOnce $path $psLine 'profiles.ps1' "`r`n" }
    Add-LineOnce $bashrc $bashLine 'profiles.sh' "`n"

    $policy = Get-ProfileExecutionPolicy
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
    $stashFiles = @((Join-Path $dest '.home-credentials.json'), (Join-Path $dest '.home-account.json'))
    if (($stashFiles | Where-Object { Test-Path $_ }).Count -gt 0) {
        # forget refuses when it cannot confirm the slot holds your own login; the rest still goes.
        if ((Invoke-Python (Join-Path $dest 'cc_use.py') forget) -ne 0) {
            $quoted = ($stashFiles | ForEach-Object { "'$($_ -replace "'", "''")'" }) -join ', '
            Write-Warning "cc-use kept its stashed copy of your login (reason above). It is harmless; once your own login is back in the default slot, delete it with: Remove-Item -Force $quoted"
        }
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
# It is appended in the file's own encoding: UTF-8 bytes added to a UTF-16 profile (what
# Windows PowerShell 5.1's `>` writes) would read back as garbage.
function Add-LineOnce([string]$path, [string]$line, [string]$scriptName, [string]$newline) {
    if (Test-Path $path) {
        $text = Get-Content $path -Raw
        if ($text -and ($text.Contains($marker) -or $text.Contains(".claude-profiles\$scriptName") -or $text.Contains(".claude-profiles/$scriptName"))) {
            return
        }
        $encoding = Get-BomEncoding $path
        if (-not $encoding) { $encoding = New-Object System.Text.UTF8Encoding $false }
    } else {
        New-Item -ItemType File -Force -Path $path | Out-Null
        # A BOM makes 5.1 read a non-ASCII folder name as UTF-8; bash would choke on one.
        $encoding = New-Object System.Text.UTF8Encoding ($newline -eq "`r`n")
    }
    [System.IO.File]::AppendAllText($path, $newline + $line + $newline, $encoding)
    Write-Output "added to ${path}: $line"
}

# Rewrites the file in place, so a symlinked profile stays a symlink, and in its own encoding
# and line endings, so the rest of the file comes back byte for byte.
function Remove-MarkedLines([string]$path) {
    if (-not (Test-Path $path)) { return }
    # Latin-1 maps each byte to one character, so a file with no BOM (UTF-8 or the ANSI code
    # page, which cannot be told apart) round-trips exactly.
    $encoding = Get-BomEncoding $path
    if (-not $encoding) { $encoding = [System.Text.Encoding]::GetEncoding(28591) }
    $text = [System.IO.File]::ReadAllText($path, $encoding)
    $kept = [regex]::Replace($text, '(?m)^[^\r\n]*' + [regex]::Escape($marker) + '[^\r\n]*(\r?\n|$)', '')
    if ($kept -eq $text) { return }
    $kept = $kept.TrimEnd([char[]]"`r`n")
    if ($kept) { $kept += $(if ($text.Contains("`r`n")) { "`r`n" } else { "`n" }) }
    [System.IO.File]::WriteAllText($path, $kept, $encoding)
    Write-Output "removed the claude-multi-account line from $path"
}

# The encoding a byte-order mark names, or $null when the file has none.
function Get-BomEncoding([string]$path) {
    $bytes = [System.IO.File]::ReadAllBytes($path)
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
        return New-Object System.Text.UTF8Encoding $true
    }
    if ($bytes.Length -ge 2 -and $bytes[0] -eq 0xFF -and $bytes[1] -eq 0xFE) { return [System.Text.Encoding]::Unicode }
    if ($bytes.Length -ge 2 -and $bytes[0] -eq 0xFE -and $bytes[1] -eq 0xFF) { return [System.Text.Encoding]::BigEndianUnicode }
    return $null
}

# The policy a new shell loads the profile under. Get-ExecutionPolicy alone includes this
# process's scope, which is Bypass when the installer runs as `-ExecutionPolicy Bypass -File`.
function Get-ProfileExecutionPolicy {
    foreach ($scope in 'MachinePolicy', 'UserPolicy', 'CurrentUser', 'LocalMachine') {
        $policy = Get-ExecutionPolicy -Scope $scope
        if ($policy -ne 'Undefined') { return $policy }
    }
    # Nothing set anywhere: Windows PowerShell on a desktop edition of Windows is Restricted.
    if ($PSVersionTable.PSEdition -eq 'Desktop' -and (Get-CimInstance Win32_OperatingSystem).ProductType -eq 1) {
        return 'Restricted'
    }
    return 'RemoteSigned'
}

# Returns Python's exit code; its output goes to the console.
function Invoke-Python {
    if (Get-Command py -ErrorAction SilentlyContinue) { & py -3 @args | Out-Host } else { & python @args | Out-Host }
    return $LASTEXITCODE
}

Main
