@echo off
rem Starts PC Care Kit. Keep this file in the same folder as PCCareKit.ps1.
start "" powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0PCCareKit.ps1"
