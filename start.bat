@echo off
REM Ξεκινάει τον Polymarket bot server και ανοίγει τον browser αυτόματα.
REM Διπλό κλικ σε αυτό το αρχείο.

cd /d "%~dp0"
title Polymarket Bot Server

echo ================================================
echo   Polymarket Bot - ξεκινάει...
echo   Αν κλεισει αμεσως, διαβασε το μηνυμα λαθους.
echo ================================================
echo.

REM Ελεγχος οτι υπαρχει Python
python --version >nul 2>&1
if errorlevel 1 (
  echo [ΛΑΘΟΣ] Δεν βρεθηκε το Python. Εγκατεστησε το απο https://python.org
  echo         και βαλε check "Add Python to PATH" στην εγκατασταση.
  echo.
  pause
  exit /b 1
)

REM Ανοιγει τον browser στο σωστο URL μετα απο 3 δευτερολεπτα
start "" /b cmd /c "timeout /t 3 >nul & start http://127.0.0.1:8000"

echo Server: http://127.0.0.1:8000
echo (Ανοιγει ο browser μονος του. Αν οχι, βαλε το URL χειροκινητα.)
echo Για διακοπη: κλεισε αυτο το παραθυρο ή πατα Ctrl+C.
echo.

python server.py

echo.
echo ================================================
echo   Ο server σταματησε. Διαβασε τυχον λαθος πιο πανω.
echo ================================================
pause
