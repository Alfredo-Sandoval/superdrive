@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Enable-AppleSuperDrive.ps1" -PauseOnExit %*
