<#
.SYNOPSIS
  Builds skill.zip, the marketplace upload, from the skill/ folder.

.DESCRIPTION
  Packs every file under skill/ except the marketplace listing draft
  (AGENSI_LISTING.md), the media/ folder, this script and any .zip.
  Entry names use forward slashes so the ZIP also unpacks on Linux and macOS.
  The output goes to the repository root (skill.zip, git-ignored).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File skill\build.ps1
#>
[CmdletBinding()]
param(
    [string]$Output
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

# Default output: skill.zip in the repository root (the folder above skill/).
if (-not $Output) {
    $Output = Join-Path (Split-Path -Path $PSScriptRoot -Parent) 'skill.zip'
}

# Paths are relative to skill/, with forward slashes.
$excludedFiles = @('AGENSI_LISTING.md', 'build.ps1')
$excludedDirs  = @('media')

$root = (Resolve-Path -LiteralPath $PSScriptRoot).Path.TrimEnd('\')

$entries = Get-ChildItem -LiteralPath $root -Recurse -File | ForEach-Object {
    $rel = $_.FullName.Substring($root.Length + 1).Replace('\', '/')
    [pscustomobject]@{ Path = $_.FullName; Name = $rel }
} | Where-Object {
    $top = $_.Name.Split('/')[0]
    ($excludedFiles -notcontains $_.Name) -and
    ($excludedDirs -notcontains $top) -and
    ($_.Name -notlike '*.zip')
} | Sort-Object Name

if (-not $entries) { throw "Nothing to pack under $root" }

if (Test-Path -LiteralPath $Output) { Remove-Item -LiteralPath $Output -Force }

$zip = [System.IO.Compression.ZipFile]::Open($Output, [System.IO.Compression.ZipArchiveMode]::Create)
try {
    foreach ($e in $entries) {
        [void][System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
            $zip, $e.Path, $e.Name, [System.IO.Compression.CompressionLevel]::Optimal)
    }
} finally {
    $zip.Dispose()
}

Write-Output "Wrote $Output"
$entries | ForEach-Object { '  ' + $_.Name }
