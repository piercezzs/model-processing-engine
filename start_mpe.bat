@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
title Model Processing Engine

cd /d "%~dp0"
set "SCRIPT_DIR=%CD%"
set "VENV_DIR=%SCRIPT_DIR%\.venv"
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"
set "DEPENDENCY_STAMP=%VENV_DIR%\.mpe-pyproject.sha256"
set "CHECK_ONLY=0"
set "STATUS_ONLY=0"
set "START_ONLY=0"
set "OPEN_ADMIN=1"
set "MPE_PROJECT_DIR=%SCRIPT_DIR%"

:parse_arguments
if "%~1"=="" goto arguments_done
if /I "%~1"=="--check-only" (
    set "CHECK_ONLY=1"
    set "OPEN_ADMIN=0"
    shift
    goto parse_arguments
)
if /I "%~1"=="--status-only" (
    set "STATUS_ONLY=1"
    set "OPEN_ADMIN=0"
    shift
    goto parse_arguments
)
if /I "%~1"=="--start-only" (
    set "START_ONLY=1"
    set "OPEN_ADMIN=0"
    shift
    goto parse_arguments
)
if /I "%~1"=="--no-open" (
    set "OPEN_ADMIN=0"
    shift
    goto parse_arguments
)
call :fail "Usage: start_mpe.bat [--check-only | --status-only | --start-only] [--no-open]"
exit /b 1

:arguments_done

set /a "MODE_COUNT=CHECK_ONLY+STATUS_ONLY+START_ONLY"
if !MODE_COUNT! GTR 1 (
    call :fail "Use only one of --check-only, --status-only, or --start-only"
    exit /b 1
)

if not exist "%SCRIPT_DIR%\pyproject.toml" (
    call :fail "pyproject.toml is missing from %SCRIPT_DIR%"
    exit /b 1
)

if "%STATUS_ONLY%"=="1" (
    if not exist "%VENV_PYTHON%" (
        call :fail "The local environment is not ready. Run start_mpe.bat once to repair it."
        exit /b 1
    )
    "%VENV_PYTHON%" -m model_processing_engine.cli status
    exit /b !ERRORLEVEL!
)

if "%CHECK_ONLY%"=="1" (
    call :check_environment
    if errorlevel 1 (
        call :fail "The local environment is not ready. Run start_mpe.bat once to repair it."
        exit /b 1
    )
    for /f "delims=" %%V in ('"%VENV_PYTHON%" --version 2^>^&1') do echo [MPE] Environment check passed: %%V
    "%VENV_PYTHON%" -m model_processing_engine.cli status
    exit /b !ERRORLEVEL!
)

if exist "%VENV_PYTHON%" (
    "%VENV_PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
    if errorlevel 1 (
        set "BACKUP_DIR=%VENV_DIR%.invalid-!RANDOM!!RANDOM!"
        echo [MPE] Existing .venv is incompatible; preserving it at !BACKUP_DIR!
        move "%VENV_DIR%" "!BACKUP_DIR!" >nul || (
            call :fail "Could not preserve the incompatible .venv"
            exit /b 1
        )
    )
)

if not exist "%VENV_PYTHON%" (
    call :find_python
    if not defined BASE_PYTHON (
        call :fail "Python 3.10 or newer was not found. Install Python 3.12 and try again."
        exit /b 1
    )
    echo [MPE] Creating .venv with !BASE_PYTHON!
    call !BASE_PYTHON! -m venv "%VENV_DIR%"
    if errorlevel 1 (
        call :fail "Could not create .venv"
        exit /b 1
    )
)

"%VENV_PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 (
    call :fail "The .venv Python must be 3.10 or newer"
    exit /b 1
)

call :check_environment
if not errorlevel 1 (
    echo [MPE] Reusing the ready .venv; pyproject.toml is unchanged
) else if not exist "%DEPENDENCY_STAMP%" (
    call :check_runtime_dependencies
    if not errorlevel 1 (
        echo [MPE] Adopting the existing ready .venv and recording its dependency state
        call :record_dependency_digest
        if errorlevel 1 (
            call :fail "Could not record the dependency state"
            exit /b 1
        )
    ) else (
        call :repair_environment
        if errorlevel 1 exit /b !ERRORLEVEL!
    )
) else (
    call :repair_environment
    if errorlevel 1 exit /b !ERRORLEVEL!
)

echo [MPE] Starting the managed service
"%VENV_PYTHON%" -m model_processing_engine.cli start
if errorlevel 1 exit /b !ERRORLEVEL!
if "%START_ONLY%"=="1" exit /b 0
"%VENV_PYTHON%" -m model_processing_engine.cli status
if errorlevel 1 exit /b !ERRORLEVEL!
if "%OPEN_ADMIN%"=="1" (
    echo [MPE] Opening the local management page
    "%VENV_PYTHON%" -m model_processing_engine.cli admin
)
exit /b 0

:find_python
set "BASE_PYTHON="
where py >nul 2>nul
if not errorlevel 1 (
    for %%V in (3.13 3.12 3.11 3.10) do (
        py -%%V -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
        if not errorlevel 1 if not defined BASE_PYTHON set "BASE_PYTHON=py -%%V"
    )
)
if not defined BASE_PYTHON (
    where python3 >nul 2>nul
    if not errorlevel 1 (
        python3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
        if not errorlevel 1 set "BASE_PYTHON=python3"
    )
)
if not defined BASE_PYTHON (
    where python >nul 2>nul
    if not errorlevel 1 (
        python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
        if not errorlevel 1 set "BASE_PYTHON=python"
    )
)
exit /b 0

:check_environment
call :check_runtime_dependencies
if errorlevel 1 exit /b 1
if not exist "%DEPENDENCY_STAMP%" exit /b 1
"%VENV_PYTHON%" -c "import hashlib, pathlib; manifest=pathlib.Path('pyproject.toml'); stamp=pathlib.Path(r'%DEPENDENCY_STAMP%'); raise SystemExit(0 if stamp.read_text(encoding='ascii').strip().lower() == hashlib.sha256(manifest.read_bytes()).hexdigest() else 1)" >nul 2>nul || exit /b 1
exit /b 0

:check_runtime_dependencies
if not exist "%VENV_PYTHON%" exit /b 1
"%VENV_PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul || exit /b 1
"%VENV_PYTHON%" -c "import fastapi, jsonschema, model_processing_engine, pydantic, uvicorn" >nul 2>nul || exit /b 1
"%VENV_PYTHON%" -m pip check >nul 2>nul || exit /b 1
exit /b 0

:record_dependency_digest
"%VENV_PYTHON%" -c "import hashlib, pathlib; manifest=pathlib.Path('pyproject.toml'); pathlib.Path(r'%DEPENDENCY_STAMP%').write_text(hashlib.sha256(manifest.read_bytes()).hexdigest() + '\n', encoding='ascii')"
exit /b 0

:repair_environment
"%VENV_PYTHON%" -m pip --version >nul 2>nul
if errorlevel 1 (
    "%VENV_PYTHON%" -m ensurepip --upgrade
    if errorlevel 1 (
        call :fail "Could not repair pip in .venv"
        exit /b 1
    )
)
echo [MPE] Installing or refreshing dependencies from pyproject.toml
"%VENV_PYTHON%" -m pip install --disable-pip-version-check -e "%SCRIPT_DIR%"
if errorlevel 1 (
    call :fail "Dependency installation failed"
    exit /b 1
)
call :record_dependency_digest
if errorlevel 1 (
    call :fail "Could not record the dependency state"
    exit /b 1
)
call :check_environment
if errorlevel 1 (
    call :fail "The repaired environment did not pass validation"
    exit /b 1
)
exit /b 0

:fail
echo [MPE] ERROR: %~1 1>&2
exit /b 0
