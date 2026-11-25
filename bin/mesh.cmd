@echo off
setlocal EnableExtensions

for %%I in ("%~dp0..") do set "INSTALL_ROOT=%%~fI"

if not defined _MESH_ORIG_PYTHONPATH (
  if defined PYTHONPATH (
    set "_MESH_ORIG_PYTHONPATH=%PYTHONPATH%"
  ) else (
    set "_MESH_ORIG_PYTHONPATH=__UNSET__"
  )
)

set "PYTHONPATH=%INSTALL_ROOT%"

if defined MESH_PYTHON goto :run_mesh_python

set "_MESH_PYTHON_BIN="
for /f "delims=" %%P in ('where python.exe 2^>nul') do (
  if not defined _MESH_PYTHON_BIN (
    set "_MESH_CAND=%%~fP"
    call :pick_python
  )
)
if defined _MESH_PYTHON_BIN goto :run_python_bin

where py.exe >nul 2>nul
if %ERRORLEVEL% equ 0 goto :run_py

python -P -s -m mesh %*
exit /b %ERRORLEVEL%

:pick_python
if not defined _MESH_CAND exit /b 0
if /i not "%_MESH_CAND:\Microsoft\WindowsApps\=%"=="%_MESH_CAND%" (
  set "_MESH_CAND="
  exit /b 0
)
set "_MESH_PYTHON_BIN=%_MESH_CAND%"
set "_MESH_CAND="
exit /b 0

:run_mesh_python
"%MESH_PYTHON%" -P -s -m mesh %*
exit /b %ERRORLEVEL%

:run_python_bin
"%_MESH_PYTHON_BIN%" -P -s -m mesh %*
exit /b %ERRORLEVEL%

:run_py
py.exe -3 -P -s -m mesh %*
exit /b %ERRORLEVEL%
