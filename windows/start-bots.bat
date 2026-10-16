@echo off
rem NYN sandbox starter bots (PAPER MONEY ONLY). They idle outside market hours; restart on exit.
cd /d "%~dp0.."
chcp 65001 >nul
rem logs go to a file, where Python would otherwise write cp1252 and fail on the rupee sign
set PYTHONUTF8=1
title NYN Sandbox bots
if not exist data\logs mkdir data\logs
:loop
start "" /B /BELOWNORMAL /WAIT ".venv\Scripts\python.exe" bots.py >> data\logs\bots.out 2>&1
ping -n 61 127.0.0.1 >nul
goto loop
