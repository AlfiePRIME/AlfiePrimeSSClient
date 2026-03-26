@echo off
setlocal EnableDelayedExpansion

title AlfiePRIME Musiciser Uninstaller
color 0C

echo.
echo  ============================================================
echo    A L F I E P R I M E   M U S I C I S E R   U N I N S T A L L
echo  ============================================================
echo.

set /p "CONFIRM=  Are you sure you want to uninstall? (y/N): "
if /i not "!CONFIRM!"=="y" (
    echo  Cancelled.
    pause
    exit /b 0
)

echo.

:: --- Find Python ---
set "PYTHON_CMD="
where python >nul 2>&1 && set "PYTHON_CMD=python"
if not defined PYTHON_CMD (
    where python3 >nul 2>&1 && set "PYTHON_CMD=python3"
)
if not defined PYTHON_CMD (
    where py >nul 2>&1 && set "PYTHON_CMD=py -3"
)

:: --- Uninstall via pipx ---
echo  [1/2] Removing AlfiePRIME Musiciser...
if defined PYTHON_CMD (
    %PYTHON_CMD% -m pipx uninstall alfieprime-musiciser 2>nul
) else (
    pipx uninstall alfieprime-musiciser 2>nul
)
echo  [OK] Application removed

:: --- Remove shortcuts ---
echo  [2/2] Removing shortcuts...
set "DESKTOP=%USERPROFILE%\Desktop"
set "STARTMENU=%APPDATA%\Microsoft\Windows\Start Menu\Programs"

if exist "%DESKTOP%\AlfiePRIME Musiciser.lnk" del "%DESKTOP%\AlfiePRIME Musiciser.lnk"
if exist "%DESKTOP%\AlfiePRIME Musiciser.bat" del "%DESKTOP%\AlfiePRIME Musiciser.bat"
if exist "%STARTMENU%\AlfiePRIME Musiciser.lnk" del "%STARTMENU%\AlfiePRIME Musiciser.lnk"
echo  [OK] Shortcuts removed

echo.
echo  ============================================================
echo    UNINSTALL COMPLETE
echo  ============================================================
echo.
echo    Config files are still at:
echo      %%USERPROFILE%%\.config\alfieprime-musiciser\
echo    Delete that folder manually if you want a clean removal.
echo.
echo  Press any key to exit...
pause >nul
