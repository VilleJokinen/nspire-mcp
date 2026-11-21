<#
.SYNOPSIS
  Set up nspire-mcp on this computer: install Python dependencies, register the
  MCP server with Claude Code, and create a desktop shortcut.

.DESCRIPTION
  Safe to run again (re-registers and overwrites the shortcut).
  Run from anywhere:   powershell -ExecutionPolicy Bypass -File setup.ps1

.PARAMETER InstallUv
  Install uv with winget if it is missing (otherwise the script stops and says how).
.PARAMETER Scope
  Claude Code scope for the MCP server: user (default, all projects), local or project.
  local and project register it for one project folder only, given with -ProjectDir.
.PARAMETER ProjectDir
  The project folder for -Scope local or project (required for those scopes).
  local: only you, in that folder. project: written to that folder's .mcp.json,
  which holds this computer's paths, so think twice before committing it.
.PARAMETER NoMcp
  Don't register the MCP server with Claude Code.
.PARAMETER NoShortcut
  Don't create the "TI-Nspire bridge" desktop shortcut.
.PARAMETER Startup
  Also put the shortcut in the Startup folder, so the hotkey starts at login.
#>
param(
    [switch]$InstallUv,
    [ValidateSet('user', 'local', 'project')][string]$Scope = 'user',
    [string]$ProjectDir,
    [switch]$NoMcp,
    [switch]$NoShortcut,
    [switch]$Startup
)

# Continue, not Stop: Windows PowerShell 5.1 turns stderr from uv/claude into errors.
$ErrorActionPreference = 'Continue'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$ok = $true

function Step($msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }
function Good($msg) { Write-Host "   OK   $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "   WARN $msg" -ForegroundColor Yellow; $script:ok = $false }

# local and project scope belong to one project folder: claude records them for
# the folder it runs in, so it must run there, not wherever this script was started.
if (-not $NoMcp -and $Scope -ne 'user') {
    if (-not $ProjectDir) {
        Write-Host "   -Scope $Scope registers the MCP server for one project folder. Say which with" -ForegroundColor Red
        Write-Host "   -ProjectDir <folder>, or use the default -Scope user (all projects)." -ForegroundColor Red
        exit 1
    }
    if (-not (Test-Path -LiteralPath $ProjectDir -PathType Container)) {
        Write-Host "   -ProjectDir: folder not found: $ProjectDir" -ForegroundColor Red
        exit 1
    }
    $ProjectDir = (Resolve-Path -LiteralPath $ProjectDir).Path
}
elseif ($ProjectDir) {
    Write-Host "   (-ProjectDir is ignored: it only applies to -Scope local or project.)" -ForegroundColor Yellow
    $ProjectDir = $null
}

# ------------------------------------------------------------------ TI-Nspire
Step 'TI-Nspire desktop software'
$nspireExe = Get-ChildItem -Path "$env:ProgramFiles\TI Education", "${env:ProgramFiles(x86)}\TI Education" `
    -Filter 'TI-Nspire*.exe' -Recurse -Depth 2 -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -notmatch 'uninst|update|helper' } | Select-Object -First 1
if ($nspireExe) { Good $nspireExe.FullName }
else { Warn 'Not found under Program Files\TI Education. Install TI-Nspire CX (CAS) Student or Teacher Software; the bridge needs it running.' }

$tnsAssoc = (cmd /c assoc .tns 2>$null)
if ($tnsAssoc) { Good ".tns files open with: $($tnsAssoc -replace '^.*=','')" }
else { Warn '.tns files have no default app; double-clicking nspire_bridge.tns (and the shortcut) won''t open TI-Nspire.' }

# ------------------------------------------------------------------ uv
Step 'uv (Python package runner)'
$uvCmd = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uvCmd -and $InstallUv) {
    Write-Host '   Installing uv with winget...'
    winget install --id astral-sh.uv -e --accept-source-agreements --accept-package-agreements | Out-Host
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
    $uvCmd = Get-Command uv -ErrorAction SilentlyContinue
}
if (-not $uvCmd) {
    Write-Host '   uv is not installed. Install it with:  winget install astral-sh.uv' -ForegroundColor Red
    Write-Host '   (or rerun this script with -InstallUv), then open a new terminal and rerun.' -ForegroundColor Red
    exit 1
}
$uv = $uvCmd.Source
$uvw = Join-Path (Split-Path $uv) 'uvw.exe'
Good $uv
if (-not (Test-Path $uvw)) { Warn "uvw.exe not found next to uv.exe; the shortcut will use uv.exe and show a console."; $uvw = $uv }

# ------------------------------------------------------------------ dependencies
Step 'Python dependencies (first run downloads Python 3.13, SymPy, pywin32...)'
foreach ($s in 'server.py', 'hotkey.py', 'launch.py', 'selftest.py') {
    & $uv sync --script (Join-Path $Here $s) --quiet
    if ($LASTEXITCODE -ne 0) { Warn "uv sync failed for $s"; } else { Good $s }
}

# ------------------------------------------------------------------ MCP registration
$server = Join-Path $Here 'server.py'
$addArgs = @('mcp', 'add', '-s', $Scope, 'nspire', '--', $uv, 'run', '--script', $server)
if (-not $NoMcp) {
    $where = if ($ProjectDir) { ", folder: $ProjectDir" } else { '' }
    Step "Register MCP server with Claude Code (scope: $Scope$where)"
    $claude = (Get-Command claude -ErrorAction SilentlyContinue).Source
    if (-not $claude) {
        # The desktop app bundles a CLI here; take the newest.
        $claude = Get-ChildItem "$env:APPDATA\Claude\claude-code" -Filter claude.exe -Recurse -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName
    }
    if ($claude) {
        if ($ProjectDir) { Push-Location -LiteralPath $ProjectDir }
        try {
            & $claude mcp remove -s $Scope nspire 2>&1 | Out-Null   # fine if it wasn't registered
            & $claude @addArgs 2>&1 | Out-Host
            $added = $LASTEXITCODE -eq 0
        }
        finally {
            if ($ProjectDir) { Pop-Location }
        }
        if ($added) { Good "registered with $claude" } else { Warn 'claude mcp add failed (see above).' }
    }
    else {
        Warn 'Claude Code CLI not found. Register by hand:'
        if ($ProjectDir) { Write-Host "   (run it in $ProjectDir)" }
        Write-Host ("   claude " + (($addArgs | ForEach-Object { if ($_ -match '\s') { "`"$_`"" } else { $_ } }) -join ' '))
    }
}

# ------------------------------------------------------------------ shortcut
function New-Shortcut($path) {
    $sh = New-Object -ComObject WScript.Shell
    $s = $sh.CreateShortcut($path)
    $s.TargetPath = $uvw
    $s.Arguments = "run --gui-script `"$(Join-Path $Here 'launch.py')`""
    $s.WorkingDirectory = $Here
    if ($nspireExe) { $s.IconLocation = "$($nspireExe.FullName),0" }
    $s.Description = 'Open nspire_bridge.tns and start the LaTeX hotkey (Ctrl+Alt+Shift+L)'
    $s.Save()
    Good $path
}
if (-not $NoShortcut) {
    Step 'Desktop shortcut'
    New-Shortcut (Join-Path ([Environment]::GetFolderPath('Desktop')) 'TI-Nspire bridge.lnk')
}
if ($Startup) {
    Step 'Startup shortcut'
    New-Shortcut (Join-Path ([Environment]::GetFolderPath('Startup')) 'TI-Nspire bridge.lnk')
}

# ------------------------------------------------------------------ done
Step 'Next'
Write-Host '   1. Open nspire_bridge.tns in TI-Nspire (or use the shortcut). The page must show'
Write-Host '      "NSPIRE BRIDGE  tick N" with N counting up.'
Write-Host "   2. Check it:  uv run --script `"$(Join-Path $Here 'selftest.py')`""
Write-Host '   3. Restart Claude Code; "claude mcp list" should show nspire connected.'
Write-Host ''
Write-Host '   Claude desktop app, Chat tab (optional): add this to %APPDATA%\Claude\claude_desktop_config.json'
$json = @{ mcpServers = @{ nspire = @{ command = $uv; args = @('run', '--script', $server) } } } | ConvertTo-Json -Depth 4
Write-Host $json
if (-not $ok) { Write-Host "`nFinished with warnings (above)." -ForegroundColor Yellow }
