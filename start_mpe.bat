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
if /I "%~1"=="--no-open" (
    set "OPEN_ADMIN=0"
    shift
    goto parse_arguments
)
call :fail "Usage: start_mpe.bat [--check-only] [--no-open]"
exit /b 1

:arguments_done

if not exist "%SCRIPT_DIR%\pyproject.toml" (
    call :fail "pyproject.toml is missing from %SCRIPT_DIR%"
    exit /b 1
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
) else (
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

    call :dependency_digest
    if not defined CURRENT_DIGEST (
        call :fail "Could not compute the dependency state"
        exit /b 1
    )
    > "%DEPENDENCY_STAMP%" echo !CURRENT_DIGEST!

    call :check_environment
    if errorlevel 1 (
        call :fail "The repaired environment did not pass validation"
        exit /b 1
    )
)

echo [MPE] Starting the managed service
"%VENV_PYTHON%" -m model_processing_engine.cli start
if errorlevel 1 exit /b !ERRORLEVEL!
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
if not exist "%VENV_PYTHON%" exit /b 1
"%VENV_PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul || exit /b 1
"%VENV_PYTHON%" -c "import fastapi, jsonschema, model_processing_engine, pydantic, uvicorn" >nul 2>nul || exit /b 1
"%VENV_PYTHON%" -m pip check >nul 2>nul || exit /b 1
if not exist "%DEPENDENCY_STAMP%" exit /b 1
call :dependency_digest
if not defined CURRENT_DIGEST exit /b 1
set "SAVED_DIGEST="
set /p SAVED_DIGEST=<"%DEPENDENCY_STAMP%"
if /I not "!CURRENT_DIGEST!"=="!SAVED_DIGEST!" exit /b 1
exit /b 0

:dependency_digest
set "CURRENT_DIGEST="
for /f "delims=" %%H in ('"%VENV_PYTHON%" -c "import hashlib, pathlib; print(hashlib.sha256(pathlib.Path('pyproject.toml').read_bytes()).hexdigest())"') do set "CURRENT_DIGEST=%%H"
exit /b 0

:fail
echo [MPE] ERROR: %~1 1>&2
exit /b 0
