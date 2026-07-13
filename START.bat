@echo off
title ManyFaces
cd /d "%~dp0"
echo ============================================================
echo   ManyFaces
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
