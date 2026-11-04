@echo off
cd /d %~dp0
rem Replay saved days only (no shioaji login, no power plan change, no auto shutdown)
if exist "%USERPROFILE%\anaconda3\Scripts\activate.bat" call "%USERPROFILE%\anaconda3\Scripts\activate.bat" "%USERPROFILE%\anaconda3"
python server.py --view
pause
