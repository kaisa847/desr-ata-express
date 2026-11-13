@echo off
rem Double-click or start from PowerShell/cmd: sets up openXC7 in WSL and builds m0, m1, m2.
rem Asks once for the Linux password (sudo). Log: build\openxc7\setup.log
rem The repository path is taken from this file's location (wsl --cd translates the Windows path).
wsl -d Ubuntu-22.04 --cd "%~dp0." -- bash -c "sed -i 's/\r$//' scripts/openxc7_setup.sh scripts/openxc7_build.sh && bash scripts/openxc7_setup.sh %*"
echo.
echo Done. Result and log are shown above and in build\openxc7\setup.log
pause
