@echo off
setlocal

cd /d "%~dp0"

set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
set "WEBUI_HOST=%WEBUI_HOST%"
if not defined WEBUI_HOST set "WEBUI_HOST=127.0.0.1"

if not exist "%PYTHON_EXE%" (
    echo [ERROR] Project virtual environment not found:
    echo         "%PYTHON_EXE%"
    echo Create .venv and install requirements.txt first.
    pause
    exit /b 1
)

if not exist "%~dp0web.py" (
    echo [ERROR] WebUI entry point not found: "%~dp0web.py"
    pause
    exit /b 1
)

echo [INFO] Checking port 5000...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ErrorActionPreference = 'Stop';" ^
    "$listeners = @(Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue);" ^
    "$ownerIds = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique);" ^
    "if ($ownerIds.Count -eq 0) { exit 0 };" ^
    "if ($ownerIds.Count -ne 1) { Write-Host '[ERROR] Port 5000 has multiple listener owners:' ($ownerIds -join ', '); exit 2 };" ^
    "$ownerId = [int]$ownerIds[0];" ^
    "$process = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $ownerId) -ErrorAction SilentlyContinue;" ^
    "if (-not $process) { Write-Host ('[ERROR] Listener PID ' + $ownerId + ' no longer exists.'); exit 3 };" ^
    "$commandLine = [string]$process.CommandLine;" ^
    "$entryPath = [IO.Path]::GetFullPath((Join-Path (Get-Location) 'web.py'));" ^
    "$entryNamePattern = '(?i)(^|\s)web\.py(?=\s|$)';" ^
    "$portPattern = '(?i)--port\s+(?:' + [char]34 + ')?5000(?:' + [char]34 + ')?(?:\s|$)';" ^
    "if (($commandLine -notmatch ('(?i)' + [regex]::Escape($entryPath)) -and $commandLine -notmatch $entryNamePattern) -or $commandLine -notmatch $portPattern) { Write-Host ('[ERROR] Port 5000 belongs to another process. PID=' + $ownerId); Write-Host ('        CommandLine: ' + $commandLine); exit 4 };" ^
    "Write-Host ('[INFO] Stopping existing WebUI. PID=' + $ownerId);" ^
    "Stop-Process -Id $ownerId -Force;" ^
    "$deadline = (Get-Date).AddSeconds(15);" ^
    "do { Start-Sleep -Milliseconds 250; $remaining = @(Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue) } while ($remaining.Count -gt 0 -and (Get-Date) -lt $deadline);" ^
    "if ($remaining.Count -gt 0) { Write-Host '[ERROR] Port 5000 was not released within 15 seconds.'; exit 5 };" ^
    "Write-Host '[INFO] Existing WebUI stopped.'"

if errorlevel 1 (
    echo.
    echo [ERROR] Unable to safely restart WebUI on port 5000.
    pause
    exit /b 1
)

echo [INFO] Starting WebUI at http://%WEBUI_HOST%:5000
echo [INFO] Closing this window will stop the WebUI.
echo.

"%PYTHON_EXE%" "%~dp0web.py" --host "%WEBUI_HOST%" --port 5000 %*
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo [ERROR] WebUI exited with code %EXIT_CODE%.
    pause
)

exit /b %EXIT_CODE%
