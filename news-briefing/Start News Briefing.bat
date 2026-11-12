@echo off
title Puppet Feeds Local
cd /d "%~dp0"
python --version >nul 2>&1
if errorlevel 1 (
  echo Python is not installed on this PC yet.
  echo.
  echo Install it by running this command in a terminal, then run this file again:
  echo.
  echo     winget install Python.Python.3.13
  echo.
  pause
  exit /b
)
start "Puppet Feeds Local" /min python "%~dp0news_briefing.py"
