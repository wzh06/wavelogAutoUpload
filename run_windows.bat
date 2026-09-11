@echo off
setlocal
set "APP_DIR=%~dp0"
pushd "%APP_DIR%" >nul 2>&1
if errorlevel 1 goto :path_error

rem Use UTF-8 for Chinese output in Windows CMD.
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

if not exist ".venv\Scripts\python.exe" (
  where py >nul 2>&1
  if not errorlevel 1 (
    echo Creating virtual environment with Python Launcher...
    py -3 -m venv .venv
  ) else (
    where python >nul 2>&1
    if errorlevel 1 goto :python_error
    echo Creating virtual environment with python...
    python -m venv .venv
  )
)

if not exist ".venv\Scripts\python.exe" goto :venv_error

echo Installing or checking Python dependencies...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :pip_error

echo Starting Wavelog ADI Auto Upload on http://0.0.0.0:10086
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 10086
set "APP_EXIT=%ERRORLEVEL%"
if not "%APP_EXIT%"=="0" goto :server_error
popd
exit /b %APP_EXIT%

:path_error
echo ERROR: Cannot access the application directory:
echo %APP_DIR%
goto :failed

:python_error
echo ERROR: Python was not found.
echo Install Python 3.10 or newer and enable the Python Launcher, then run this file again.
goto :failed

:venv_error
echo ERROR: Virtual environment creation failed.
echo Please verify that Python 3.10 or newer is installed and available in PATH.
goto :failed

:pip_error
echo ERROR: Dependency installation failed.
echo Check your network connection or Python package index settings.
goto :failed

:server_error
echo ERROR: The FastAPI server stopped with exit code %APP_EXIT%.
goto :failed

:failed
echo.
echo Press any key to close this window...
pause >nul
popd
exit /b 1
