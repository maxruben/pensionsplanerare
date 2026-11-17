@echo off
REM Starta Pension Planner
REM Dubbelklicka pa filen for att starta appen.

setlocal
cd /d "%~dp0"

REM Verifiera att Python finns
where python >nul 2>&1
if errorlevel 1 (
    echo FEL: python hittades inte i PATH.
    echo Ladda ner Python fran https://www.python.org/downloads/
    echo och bocka i "Add Python to PATH" vid installationen.
    echo Kor sedan install.bat innan du startar appen.
    pause
    exit /b 1
)

REM Starta appen via pythonw sa ingen konsol visas
start "" pythonw pension_planner.py
