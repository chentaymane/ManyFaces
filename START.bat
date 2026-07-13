@echo off
title Anti-Detect Manager
cd /d "%~dp0"
echo ============================================================
echo   Anti-Detect Manager
echo ============================================================
echo   Starting... your browser will open automatically.
echo   Keep THIS window open while using the app.
echo   Close this window to stop the app.
echo ============================================================
echo.
python run.py
echo.
echo The app has stopped. Press any key to close.
pause >nul
