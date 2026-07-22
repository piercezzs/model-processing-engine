@echo off
setlocal EnableExtensions
chcp 65001 >nul
title Stop Model Processing Engine

cd /d "%~dp0"
set "VENV_PYTHON=%CD%\.venv\Scripts\python.exe"

if not exist "%VENV_PYTHON%" (
    echo [MPE] ERROR: The MPE .venv is missing. Run start_mpe.bat first. 1>&2
    exit /b 1
)

"%VENV_PYTHON%" -c "import model_processing_engine" >nul 2>nul
if errorlevel 1 (
    echo [MPE] ERROR: MPE is not installed in .venv. Run start_mpe.bat first. 1>&2
    exit /b 1
)

echo [MPE] Stopping the verified managed service
"%VENV_PYTHON%" -m model_processing_engine.cli stop
if errorlevel 1 exit /b %ERRORLEVEL%
"%VENV_PYTHON%" -m model_processing_engine.cli status
exit /b %ERRORLEVEL%
