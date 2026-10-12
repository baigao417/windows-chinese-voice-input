@echo off
setlocal
title Windows Typeless Voice Input - Daily Safe
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run.ps1" --daily
