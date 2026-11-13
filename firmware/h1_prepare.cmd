@echo off
rem One-time setup: usbipd-win (forwards USB devices to WSL) and Linux tools for the tests H1/H2.
rem Asks for Windows administrator rights (winget) and the Linux password (apt).
echo === Installing usbipd-win ===
winget install --exact --id dorssel.usbipd-win --accept-source-agreements --accept-package-agreements
echo.
echo === Installing Linux tools in WSL (hdparm, smartmontools, sg3-utils, usbutils) ===
wsl -d Ubuntu-22.04 -- sudo apt-get update
wsl -d Ubuntu-22.04 -- sudo apt-get install -y hdparm smartmontools sg3-utils usbutils xxd
echo.
echo Done. Close this window and then start h1_test.cmd.
pause
