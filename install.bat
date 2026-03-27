@echo off
setlocal EnableDelayedExpansion

:: ============================================================================
::  AlfiePRIME Musiciser - One-Click Windows Installer
:: ============================================================================
::
::  What this does:
::    1. Checks for Python 3.12+ (offers to install if missing)
::    2. Installs pipx (isolated app installer)
::    3. Installs alfieprime-musiciser and all dependencies
::    4. Creates a desktop shortcut and Start Menu entry
::
::  After install, just double-click "AlfiePRIME Musiciser" on your desktop.
:: ============================================================================

title AlfiePRIME Musiciser Installer
color 0D

echo.
echo  ============================================================
echo    A L F I E P R I M E   M U S I C I S E R   I N S T A L L
echo  ============================================================
echo.

:: --- Check for admin (not required, but warn about PATH) ---
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo  [!] Running without admin rights.
    echo      If Python isn't on PATH, you may need to restart
    echo      your terminal after install.
    echo.
)

:: --- Locate Python 3.12+ ---
set "PYTHON_CMD="

:: Try 'python' first
where python >nul 2>&1
if %errorlevel% equ 0 (
    for /f "tokens=*" %%v in ('python -c "import sys; print(str(sys.version_info.major)+'.'+str(sys.version_info.minor))" 2^>nul') do set "PY_VER=%%v"
    for /f "tokens=1,2 delims=." %%a in ("!PY_VER!") do (
        if %%a geq 3 if %%b geq 12 (
            set "PYTHON_CMD=python"
        )
    )
)

:: Try 'python3' if python didn't work
if not defined PYTHON_CMD (
    where python3 >nul 2>&1
    if %errorlevel% equ 0 (
        for /f "tokens=*" %%v in ('python3 -c "import sys; print(str(sys.version_info.major)+'.'+str(sys.version_info.minor))" 2^>nul') do set "PY_VER=%%v"
        for /f "tokens=1,2 delims=." %%a in ("!PY_VER!") do (
            if %%a geq 3 if %%b geq 12 (
                set "PYTHON_CMD=python3"
            )
        )
    )
)

:: Try py launcher
if not defined PYTHON_CMD (
    where py >nul 2>&1
    if %errorlevel% equ 0 (
        for /f "tokens=*" %%v in ('py -3 -c "import sys; print(str(sys.version_info.major)+'.'+str(sys.version_info.minor))" 2^>nul') do set "PY_VER=%%v"
        for /f "tokens=1,2 delims=." %%a in ("!PY_VER!") do (
            if %%a geq 3 if %%b geq 12 (
                set "PYTHON_CMD=py -3"
            )
        )
    )
)

if not defined PYTHON_CMD (
    echo  [X] Python 3.12 or newer is required but was not found.
    echo.
    echo  Opening the Python download page...
    echo  Please install Python and TICK "Add to PATH" during setup.
    echo  Then re-run this installer.
    echo.
    start https://www.python.org/downloads/
    echo  Press any key to exit...
    pause >nul
    exit /b 1
)

echo  [OK] Found Python !PY_VER! (!PYTHON_CMD!)
echo.

:: --- Determine install source ---
:: If this script is next to pyproject.toml, install from local source.
:: Otherwise, prompt for a path or git URL.
set "INSTALL_SOURCE="
if exist "%~dp0pyproject.toml" (
    set "INSTALL_SOURCE=%~dp0."
    echo  [OK] Found project source in: %~dp0
) else (
    echo  [!] pyproject.toml not found next to this installer.
    echo      Place this script in the AlfiePRIME-Musiciser folder,
    echo      or enter the path/git URL below.
    echo.
    set /p "INSTALL_SOURCE=  Install source (path or git URL): "
)

if not defined INSTALL_SOURCE (
    echo  [X] No install source provided. Exiting.
    pause
    exit /b 1
)

echo.

:: --- Install/upgrade pip ---
echo  [1/4] Ensuring pip is up to date...
%PYTHON_CMD% -m pip install --upgrade pip --quiet 2>nul
if %errorlevel% neq 0 (
    echo  [!] pip upgrade had warnings, continuing...
)

:: --- Install pipx ---
echo  [2/4] Installing pipx...
%PYTHON_CMD% -m pip install --user pipx --quiet 2>nul
if %errorlevel% neq 0 (
    echo  [!] pipx install had warnings, trying to continue...
)
%PYTHON_CMD% -m pipx ensurepath >nul 2>&1

:: --- Install the app via pipx ---
echo  [3/4] Installing AlfiePRIME Musiciser (this may take a minute)...
%PYTHON_CMD% -m pipx install --force "%INSTALL_SOURCE%"
if %errorlevel% neq 0 (
    echo.
    echo  [X] Installation failed. Check the errors above.
    echo      Common fixes:
    echo        - Make sure you have a C compiler for native deps
    echo          (install Visual Studio Build Tools)
    echo        - Try: pip install --user alfieprime-musiciser
    echo.
    pause
    exit /b 1
)
echo  [OK] Installation complete!
echo.

:: --- Try to install winsdk for Windows media key support (optional) ---
echo  [3b/4] Installing Windows media key support (optional)...
%PYTHON_CMD% -m pipx inject alfieprime-musiciser winsdk --quiet 2>nul
if %errorlevel% neq 0 (
    echo  [!] winsdk could not be installed (needs Visual Studio Build Tools).
    echo      Media keys/lock screen controls won't be available.
    echo      Everything else works fine. You can install later with:
    echo        pipx inject alfieprime-musiciser winsdk
    echo.
) else (
    echo  [OK] Windows media key support installed
    echo.
)

:: --- Find the installed GUI launcher (runs via pythonw, no console flash) ---
set "APP_EXE="

:: pipx installs to %USERPROFILE%\.local\bin on Windows
:: Prefer the -app GUI launcher; fall back to the console exe
if exist "%USERPROFILE%\.local\bin\alfieprime-musiciser-app.exe" (
    set "APP_EXE=%USERPROFILE%\.local\bin\alfieprime-musiciser-app.exe"
) else if exist "%USERPROFILE%\.local\bin\alfieprime-musiciser.exe" (
    set "APP_EXE=%USERPROFILE%\.local\bin\alfieprime-musiciser.exe"
)

:: Fallback: search PATH
if not defined APP_EXE (
    for /f "tokens=*" %%p in ('where alfieprime-musiciser-app 2^>nul') do set "APP_EXE=%%p"
)
if not defined APP_EXE (
    for /f "tokens=*" %%p in ('where alfieprime-musiciser 2^>nul') do set "APP_EXE=%%p"
)

:: --- Create desktop shortcut ---
echo  [4/4] Creating shortcuts...

set "DESKTOP=%USERPROFILE%\Desktop"
set "STARTMENU=%APPDATA%\Microsoft\Windows\Start Menu\Programs"

:: Use PowerShell to create proper .lnk shortcuts
if defined APP_EXE if exist "!APP_EXE!" (
    :: Desktop shortcut
    powershell -NoProfile -Command ^
        "$ws = New-Object -ComObject WScript.Shell; ^
         $s = $ws.CreateShortcut('%DESKTOP%\AlfiePRIME Musiciser.lnk'); ^
         $s.TargetPath = '!APP_EXE!'; ^
         $s.Description = 'AlfiePRIME Musiciser - Party Mode Music Receiver'; ^
         $s.Save()"
    echo  [OK] Desktop shortcut created

    :: Start Menu shortcut
    powershell -NoProfile -Command ^
        "$ws = New-Object -ComObject WScript.Shell; ^
         $s = $ws.CreateShortcut('%STARTMENU%\AlfiePRIME Musiciser.lnk'); ^
         $s.TargetPath = '!APP_EXE!'; ^
         $s.Description = 'AlfiePRIME Musiciser - Party Mode Music Receiver'; ^
         $s.Save()"
    echo  [OK] Start Menu shortcut created
) else (
    :: Fallback: create a .bat launcher on the desktop
    echo @echo off > "%DESKTOP%\AlfiePRIME Musiciser.bat"
    echo title AlfiePRIME Musiciser >> "%DESKTOP%\AlfiePRIME Musiciser.bat"
    echo alfieprime-musiciser %%* >> "%DESKTOP%\AlfiePRIME Musiciser.bat"
    echo if errorlevel 1 pause >> "%DESKTOP%\AlfiePRIME Musiciser.bat"
    echo  [OK] Desktop launcher created (batch file fallback)
    echo  [!] Could not locate .exe - you may need to restart your terminal
    echo      for PATH changes to take effect, then re-run this installer.
)

echo.
echo  ============================================================
echo    INSTALLATION COMPLETE!
echo  ============================================================
echo.
echo    You can now run AlfiePRIME Musiciser by:
echo      - Double-clicking the desktop shortcut
echo      - Running 'alfieprime-musiciser' in any terminal
echo      - Running 'alfieprime-musiciser --demo' to test without a server
echo.
echo    To update later:  pipx upgrade alfieprime-musiciser
echo    To uninstall:     pipx uninstall alfieprime-musiciser
echo.
echo  Press any key to exit...
pause >nul
