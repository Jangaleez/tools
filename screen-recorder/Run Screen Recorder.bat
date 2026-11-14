@echo off
title Screen Recorder
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

rem Records computer sound straight from the speakers, so Stereo Mix isn't needed
python -c "import pyaudiowpatch" >nul 2>&1
if errorlevel 1 (
  echo Installing the part that records computer sound. This only happens once...
  python -m pip install PyAudioWPatch
  if errorlevel 1 (
    echo.
    echo That didn't install, so computer sound won't be available. The app still works without it.
    timeout /t 8
  )
)

start "" pythonw "%~dp0screen_recorder.py"
