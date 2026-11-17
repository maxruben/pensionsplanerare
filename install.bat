@echo off
REM Installera Python-bibliotek för Pension Planner
REM Dubbelklicka på filen (eller kör den från terminal) för att installera.

setlocal
cd /d "%~dp0"

echo ============================================
echo Pension Planner - installation av bibliotek
echo ============================================
echo.

REM Verifiera att Python finns
where python >nul 2>&1
if errorlevel 1 (
    echo FEL: python hittades inte i PATH.
    echo.
    echo Ladda ner Python 3.9 eller senare fran https://www.python.org/downloads/
    echo Bocka i "Add Python to PATH" vid installationen.
    echo.
    pause
    exit /b 1
)

echo Python-version:
python --version
echo.

echo Installerar/uppdaterar Python-bibliotek fran requirements.txt...
echo.
python -m pip install --upgrade -r requirements.txt
if errorlevel 1 (
    echo.
    echo FEL: pip install misslyckades. Se meddelanden ovan.
    echo.
    pause
    exit /b 1
)

echo.
echo ============================================
echo Klart! Du kan nu starta appen genom att
echo dubbelklicka pa start.bat eller kora:
echo   python pension_planner.py
echo ============================================
echo.
pause
