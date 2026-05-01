@echo off
REM Downloads bcg729 (open-source G.729 codec, BSD license) from GitHub
REM Run this once before building the project.

echo === Setting up bcg729 G.729 codec ===

set "CPP_DIR=%~dp0app\src\main\cpp\bcg729"
set "TEMP_DIR=%~dp0.bcg729_tmp"

if exist "%TEMP_DIR%" rmdir /s /q "%TEMP_DIR%"
if not exist "%CPP_DIR%" mkdir "%CPP_DIR%"

echo Downloading bcg729...
git clone --depth 1 https://gitlab.ouvaton.org/ouvaton/bcg729.git "%TEMP_DIR%" 2>nul
if errorlevel 1 (
    echo GitLab failed, trying GitHub mirror...
    git clone --depth 1 https://github.com/nicovoice/bcg729.git "%TEMP_DIR%" 2>nul
    if errorlevel 1 (
        echo.
        echo ERROR: Could not download bcg729. Please manually download from:
        echo   https://gitlab.ouvaton.org/ouvaton/bcg729
        echo.
        echo Then copy the .c and .h files from src/ and include/ into:
        echo   %CPP_DIR%\
        exit /b 1
    )
)

REM Remove stub (will be replaced by real implementation)
if exist "%CPP_DIR%\bcg729_stub.c" del /q "%CPP_DIR%\bcg729_stub.c"

echo Copying source files...
if exist "%TEMP_DIR%\src\*.c" copy /y "%TEMP_DIR%\src\*.c" "%CPP_DIR%\" >nul
if exist "%TEMP_DIR%\src\*.h" copy /y "%TEMP_DIR%\src\*.h" "%CPP_DIR%\" >nul
if exist "%TEMP_DIR%\include\*.h" copy /y "%TEMP_DIR%\include\*.h" "%CPP_DIR%\" >nul

rmdir /s /q "%TEMP_DIR%"

echo.
echo === Done ===
echo Source files copied to: %CPP_DIR%
echo You can now build the project in Android Studio.
pause
