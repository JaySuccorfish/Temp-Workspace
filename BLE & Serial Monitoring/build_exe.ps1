<#
.SYNOPSIS
    Builds "Tools/BLE & Serial Monitoring/debug_monitor.py" into a portable Windows executable.

.DESCRIPTION
    One script covers every situation a developer needs:

      -NoArgs (default)   create/refresh the venv, install deps, build the portable one-file exe
      -Mode OneDir        build a folder instead (starts ~10x faster, same portability)
      -RunFromSource      skip PyInstaller entirely and just launch the .py from the venv
      -Venv               only create/refresh the venv (fastest way to get a working dev setup)
      -Clean              delete the venv and all build output, then exit

    The venv is created with whatever `py -3.14` resolves to. override with -PythonExe.
    Everything the script creates is gitignored via Tools/.gitignore.

.PARAMETER Mode
    OneFile (default) -> dist\ValtrackDebugMonitor.exe          (~15 MB, one file)
    OneDir            -> dist\ValtrackDebugMonitor\...          (folder, fast start)

.PARAMETER Version
    Optional string stamped into the exe's file-version resource, e.g. 1.4.0.

.EXAMPLE
    .\build_exe.ps1
    Rebuild the portable exe.

.EXAMPLE
    .\build_exe.ps1 -RunFromSource
    Develop against the live script -- no build step, instant iteration.

.EXAMPLE
    .\build_exe.ps1 -Mode OneDir -Version 1.5.0
    Fast-starting folder build with a version stamp.

.NOTES
    @file build_exe.ps1
    @brief Build, run or clean the BLE debug monitor's portable .exe (venv + PyInstaller), for bench PCs without Python
    @version 1.1
    @date 2026-11-07
#>

[CmdletBinding()]
param(
    [ValidateSet('OneFile', 'OneDir')]
    [string]$Mode = 'OneFile',

    [string]$Version = '',

    [switch]$RunFromSource,
    [switch]$Venv,
    [switch]$Clean,

    [string]$PythonExe = '',
    [switch]$NoSmokeTest
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Source    = Join-Path $ScriptDir 'debug_monitor.py'
$VenvDir   = Join-Path $ScriptDir '.build-venv'
$VenvPy    = Join-Path $VenvDir 'Scripts\python.exe'
$BuildDir  = Join-Path $ScriptDir '.build'
$DistDir   = Join-Path $ScriptDir 'dist'
$AppName   = 'ValtrackDebugMonitor'

# Console output helpers: a step heading, a success line, a warning line.
function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "    $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "    $msg" -ForegroundColor Yellow }

# ---------------------------------------------------------------------------------------------
# -Clean
# ---------------------------------------------------------------------------------------------
if ($Clean) {
    Write-Step 'Removing venv and build output'
    foreach ($d in @($VenvDir, $BuildDir, $DistDir)) {
        if (Test-Path $d) {
            # Guard: only ever delete inside Tools\
            $full = (Resolve-Path $d).Path
            if (-not $full.StartsWith($ScriptDir, [StringComparison]::OrdinalIgnoreCase)) {
                throw "Refusing to delete outside Tools: $full"
            }
            Remove-Item $full -Recurse -Force
            Write-Ok "removed $full"
        }
    }
    Write-Ok 'clean'
    return
}

# ---------------------------------------------------------------------------------------------
# Helper: run a native command without PowerShell 5.1 / 7.x turning stderr INFO lines into
# terminating NativeCommandError exceptions (and safe when paths contain '&' or spaces).
# ---------------------------------------------------------------------------------------------
function Invoke-NativeCapture {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    $tmpOut = [System.IO.Path]::GetTempFileName()
    $tmpErr = [System.IO.Path]::GetTempFileName()
    try {
        $quotedArgs = $Arguments | ForEach-Object {
            if ($_ -match '[\s"&]') {
                '"' + ($_ -replace '"', '\"') + '"'
            } else {
                $_
            }
        }
        $argString = $quotedArgs -join ' '
        $proc = Start-Process -FilePath $FilePath -ArgumentList $argString `
            -NoNewWindow -Wait -PassThru `
            -RedirectStandardOutput $tmpOut -RedirectStandardError $tmpErr
        $outText = if (Test-Path $tmpOut) { Get-Content $tmpOut -Raw -ErrorAction SilentlyContinue } else { '' }
        $errText = if (Test-Path $tmpErr) { Get-Content $tmpErr -Raw -ErrorAction SilentlyContinue } else { '' }
        return [PSCustomObject]@{
            ExitCode = $proc.ExitCode
            Output   = (($outText + "`n" + $errText).Trim())
        }
    } finally {
        Remove-Item $tmpOut, $tmpErr -Force -ErrorAction SilentlyContinue
    }
}

# ---------------------------------------------------------------------------------------------
# Test whether a Python executable is usable (Python >= 3.10 with venv + tkinter, not Store stub)
# ---------------------------------------------------------------------------------------------
function Test-PythonCandidate {
    param(
        [Parameter(Mandatory = $true)][string]$Exe,
        [string[]]$PrefixArgs = @()
    )
    try {
        if ($Exe -match 'WindowsApps\\python') { return $null }
        $checkScript = "import sys, venv, tkinter; sys.exit(0 if sys.version_info >= (3, 10) else 1); print(sys.executable)"
        $checkScript = "import sys, venv, tkinter`nif sys.version_info < (3, 10): raise SystemExit(1)`nprint(sys.executable)"
        $res = Invoke-NativeCapture -FilePath $Exe -Arguments ($PrefixArgs + @('-c', $checkScript))
        if ($res.ExitCode -eq 0 -and $res.Output) {
            $firstLine = ($res.Output -split "`r?`n" | Where-Object { $_.Trim() -and (Test-Path $_.Trim()) } | Select-Object -First 1)
            if ($firstLine -and ($firstLine -notmatch 'WindowsApps\\python')) {
                return $firstLine.Trim()
            }
        }
    } catch { }
    return $null
}

# ---------------------------------------------------------------------------------------------
# Resolve a base interpreter on ANY laptop:
#   1. -PythonExe when explicitly provided
#   2. py launcher (3.14 -> 3.10 -> -3) and PATH (python, python3)
#   3. Standard per-user / system install directories across Windows laptops
#   4. Automatic user-scope install via winget if Python is not installed yet
# ---------------------------------------------------------------------------------------------
function Resolve-Python {
    param([string]$Explicit)
    if ($Explicit) {
        if (-not (Test-Path $Explicit)) { throw "PythonExe not found: $Explicit" }
        return (Resolve-Path $Explicit).Path
    }

    foreach ($ver in @('-3.14', '-3.13', '-3.12', '-3.11', '-3.10', '-3')) {
        $pyCmd = Get-Command 'py' -ErrorAction SilentlyContinue
        if ($pyCmd) {
            $found = Test-PythonCandidate -Exe $pyCmd.Source -PrefixArgs @($ver)
            if ($found) { return $found }
        }
    }

    foreach ($cmdName in @('python', 'python3')) {
        Get-Command $cmdName -All -ErrorAction SilentlyContinue | ForEach-Object {
            if ($_.Source -and ($_.Source -notmatch 'WindowsApps\\python')) {
                $found = Test-PythonCandidate -Exe $_.Source
                if ($found) { return $found }
            }
        }
    }

    $searchRoots = @(
        "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
        "$env:ProgramFiles\Python3*\python.exe",
        "${env:ProgramFiles(x86)}\Python3*\python.exe",
        "C:\Python3*\python.exe",
        "$env:USERPROFILE\scoop\apps\python*\current\python.exe",
        "$env:USERPROFILE\miniconda3\python.exe",
        "$env:USERPROFILE\anaconda3\python.exe"
    )
    foreach ($pattern in $searchRoots) {
        $candidates = Get-Item $pattern -ErrorAction SilentlyContinue | Sort-Object FullName -Descending
        foreach ($cand in $candidates) {
            $found = Test-PythonCandidate -Exe $cand.FullName
            if ($found) { return $found }
        }
    }

    $winget = Get-Command 'winget' -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Warn 'No Python 3.10+ with Tkinter found on this laptop; installing Python 3.13 via winget...'
        $wgRes = Invoke-NativeCapture -FilePath $winget.Source -Arguments @(
            'install', '-e', '--id', 'Python.Python.3.13',
            '--scope', 'user', '--accept-package-agreements', '--accept-source-agreements'
        )
        if ($wgRes.ExitCode -eq 0) {
            $installed = Get-Item "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue |
                Sort-Object FullName -Descending | Select-Object -First 1
            if ($installed) {
                $found = Test-PythonCandidate -Exe $installed.FullName
                if ($found) { return $found }
            }
        }
    }

    throw 'No suitable Python (3.10+ with Tkinter/venv) found on this machine. Install Python from https://www.python.org/downloads/windows/ or pass -PythonExe <path>.'
}

# ---------------------------------------------------------------------------------------------
# -Venv / create venv: self-heal if .build-venv came from another laptop, then install deps
# ---------------------------------------------------------------------------------------------
function Ensure-Venv {
    if (Test-Path $VenvPy) {
        $health = Invoke-NativeCapture -FilePath $VenvPy -Arguments @('-c', 'import sys, tkinter, pip; print(sys.executable)')
        if ($health.ExitCode -ne 0) {
            Write-Warn "Existing venv at $VenvDir is from another machine or broken; recreating..."
            Remove-Item $VenvDir -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    if (-not (Test-Path $VenvPy)) {
        $base = Resolve-Python -Explicit $PythonExe
        Write-Step "Creating venv from $base"
        $venvRes = Invoke-NativeCapture -FilePath $base -Arguments @('-m', 'venv', $VenvDir)
        if ($venvRes.ExitCode -ne 0) {
            Write-Warn $venvRes.Output
            throw 'venv creation failed'
        }
        Write-Ok "venv at $VenvDir"
    } else {
        Write-Ok "venv exists: $VenvDir"
    }

    Write-Step 'Installing / verifying dependencies'
    [void](Invoke-NativeCapture -FilePath $VenvPy -Arguments @('-m', 'pip', 'install', '--upgrade', 'pip', '--quiet'))
    $pipRes = Invoke-NativeCapture -FilePath $VenvPy -Arguments @(
        '-m', 'pip', 'install', '-r', (Join-Path $ScriptDir 'requirements.txt'), '--quiet'
    )
    if ($pipRes.ExitCode -ne 0) {
        Write-Warn $pipRes.Output
        throw 'dependency install failed'
    }

    # bleak has no __version__ attribute -- read the installed metadata instead.
    $verRes = Invoke-NativeCapture -FilePath $VenvPy -Arguments @(
        '-c',
        "import sys, importlib.metadata as m; v = lambda n: m.version(n) if n else '?'; print(sys.version.split()[0], '| bleak', m.version('bleak'), '| pycryptodome', m.version('pycryptodome'), '| pyinstaller', m.version('pyinstaller'))"
    )
    if ($verRes.Output) {
        Write-Ok "python $($verRes.Output.Trim())"
    }
}

if ($Venv) { Ensure-Venv; Write-Ok 'venv ready'; return }

# ---------------------------------------------------------------------------------------------
# -RunFromSource
# ---------------------------------------------------------------------------------------------
if ($RunFromSource) {
    Ensure-Venv
    Write-Step "Launching $Source"
    Write-Host '    close the window to return to the prompt' -ForegroundColor DarkGray
    & $VenvPy $Source
    return
}

# ---------------------------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------------------------
if (-not (Test-Path $Source)) { throw "Source not found: $Source" }
Ensure-Venv

Write-Step "Building ($Mode)"
Get-Process -Name $AppName -ErrorAction SilentlyContinue | ForEach-Object {
    Write-Warn "Stopping running $AppName process (PID $($_.Id)) before overwriting exe..."
    Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Milliseconds 500
$pyiArgs = @(
    '-m', 'PyInstaller',
    '--noconfirm', '--clean',
    '--name', $AppName,
    '--paths', $ScriptDir,
    '--collect-submodules', 'monitor',
    '--collect-submodules', 'serial',
    '--hidden-import', 'serial',
    '--hidden-import', 'serial.tools',
    '--hidden-import', 'serial.tools.list_ports',
    '--hidden-import', 'sc2_protocol',
    '--hidden-import', 'brand_assets',
    '--distpath', $DistDir,
    '--workpath', (Join-Path $BuildDir 'build'),
    '--specpath', $BuildDir
)

if ($Mode -eq 'OneFile') { $pyiArgs += '--onefile' } else { $pyiArgs += '--onedir' }

# GUI app: no console window. Flip to '--console' while diagnosing a startup crash --
# a windowed exe that fails to import exits silently with no message.
$pyiArgs += '--windowed'

if ($Version) {
    if ($Version -notmatch '^\d+\.\d+\.\d+\.\d+$') {
        if ($Version -match '^\d+\.\d+\.\d+$') { $Version += '.0' }
        else { throw "Version must be 1.2.3 or 1.2.3.4, got '$Version'" }
    }
    $verFile = Join-Path $BuildDir 'version_info.txt'
    $verTuple = $Version -replace '\.', ', '
    New-Item -ItemType Directory -Force -Path $BuildDir | Out-Null
    @"
VSVersionInfo(
  ffi=FixedFileInfo(filevers=($verTuple), prodvers=($verTuple)),
  kids=[
    StringFileInfo([StringTable('040904B0',
      [StringStruct('FileDescription','Valtrack Debug Monitor'),
       StringStruct('FileVersion','$Version'),
       StringStruct('ProductName','Valtrack Debug Monitor'),
       StringStruct('ProductVersion','$Version')])]),
    VarFileInfo([VarStruct('Translation',[1033,1200])])
  ]
)
"@ | Set-Content -Path $verFile -Encoding ASCII
    $pyiArgs += @('--version-file', $verFile)
    Write-Ok "version $Version"
}

$pyiArgs += $Source

$pyiRes = Invoke-NativeCapture -FilePath $VenvPy -Arguments $pyiArgs
$pyiOut = $pyiRes.Output
$pyiExit = $pyiRes.ExitCode

$pyiOut -split "`r?`n" | Where-Object { $_ -match 'ERROR|Traceback|lib not found|not found:' } |
    ForEach-Object { Write-Warn $_.Trim() }

if ($pyiExit -ne 0) {
    Write-Warn '--- last 30 lines of PyInstaller output ---'
    ($pyiOut -split "`r?`n" | Select-Object -Last 30) | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
    throw "PyInstaller failed (exit $pyiExit)"
}

# ---------------------------------------------------------------------------------------------
# Locate + report
# ---------------------------------------------------------------------------------------------
if ($Mode -eq 'OneFile') {
    $artifact = Join-Path $DistDir "$AppName.exe"
    if (-not (Test-Path $artifact)) { throw "Expected $artifact" }
    $sizeMb = [math]::Round((Get-Item $artifact).Length / 1MB, 2)
    Write-Step 'Built'
    Write-Ok "$artifact  ($sizeMb MB, single file, no install)"
} else {
    $artifact = Join-Path $DistDir $AppName
    $exe = Join-Path $artifact "$AppName.exe"
    if (-not (Test-Path $exe)) { throw "Expected $exe" }
    $sizeMb = [math]::Round(((Get-ChildItem $artifact -Recurse -File | Measure-Object Length -Sum).Sum) / 1MB, 2)
    Write-Step 'Built'
    Write-Ok "$exe  ($sizeMb MB total, keep the whole folder together)"
}

# ---------------------------------------------------------------------------------------------
# Smoke test -- catches a missing hidden import, which is SILENT in a --windowed exe
# ---------------------------------------------------------------------------------------------
if (-not $NoSmokeTest) {
    Write-Step 'Smoke test (8 s)'
    $exePath = if ($Mode -eq 'OneFile') { $artifact } else { Join-Path $artifact "$AppName.exe" }
    $proc = Start-Process -FilePath $exePath -PassThru
    Start-Sleep -Seconds 8
    $live = Get-Process -Id $proc.Id -ErrorAction SilentlyContinue
    if ($live) {
        Stop-Process -Id $proc.Id -Force
        Write-Ok 'process stayed alive -> imports and Tk startup OK'
    } else {
        Write-Warn 'process exited within 8 s -- rebuild with --console to see the traceback'
        throw 'smoke test failed'
    }
}

Write-Host ''
Write-Ok "done. Distribute the artefact from: $DistDir"
