@echo off
cd /d %~dp0
if not exist config.ini copy config.example.ini config.ini >nul
rem Use Anaconda like x_ForStock.bat (plain "python" may not be on PATH)
if exist "%USERPROFILE%\anaconda3\Scripts\activate.bat" call "%USERPROFILE%\anaconda3\Scripts\activate.bat" "%USERPROFILE%\anaconda3"
python --version
if errorlevel 1 (
  echo [ERROR] python not found. Install Anaconda or Python 3.9+ first.
  pause
  exit /b 1
)
python server.py
pause
