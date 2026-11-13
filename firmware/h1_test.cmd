@echo off
rem Test H1: m1.bit on the PC via a USB-IDE adapter, READ ONLY. Procedure:
rem   1. Power the board, connect the IDE adapter to the board, do NOT plug in USB yet
rem   2. Load m1.bit via JTAG (see the command in the instructions)
rem   3. Plug in the adapter's USB cable, then start this script
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\h1_test.ps1"
pause
