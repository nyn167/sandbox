@echo off
rem NYN paper-trading sandbox server, 24/7 (PAPER MONEY ONLY). Restarts on exit: crash, deploy, daily 08:55 refresh.
cd /d "%~dp0.."
chcp 65001 >nul
title NYN Sandbox server
if not exist data\logs mkdir data\logs
:loop
start "" /B /BELOWNORMAL /WAIT ".venv\Scripts\python.exe" run.py >> data\logs\server.out 2>&1
ping -n 31 127.0.0.1 >nul
goto loop
