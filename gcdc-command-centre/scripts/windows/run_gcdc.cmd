@echo off
rem Runs one gcdc command from Task Scheduler and appends its output to logs\gcdc-YYYY-MM.log.
rem Usage: run_gcdc.cmd "C:\path\to\.venv\Scripts\gcdc.exe" refresh
setlocal
rem Capture the script folder before SHIFT (which also shifts %0).
set "HERE=%~dp0"
set "GCDC=%~1"
shift
set "ARGS="
:collect
if "%~1"=="" goto run
set "ARGS=%ARGS% %1"
shift
goto collect
:run
cd /d "%HERE%..\.."
if not exist logs mkdir logs
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM"') do set "MONTH=%%i"
set "PYTHONIOENCODING=utf-8"
echo ==== %DATE% %TIME% gcdc%ARGS% >> "logs\gcdc-%MONTH%.log"
"%GCDC%"%ARGS% >> "logs\gcdc-%MONTH%.log" 2>&1
set "RC=%ERRORLEVEL%"
echo ==== exit %RC% >> "logs\gcdc-%MONTH%.log"
exit /b %RC%
