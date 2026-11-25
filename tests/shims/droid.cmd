@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "TRIPWIRE=%_TRIPWIRE_FILE%"
if not defined TRIPWIRE if defined TEMP set "TRIPWIRE=%TEMP%\mesh-tripwire-hit.log"
if not defined TRIPWIRE if defined TMP set "TRIPWIRE=%TMP%\mesh-tripwire-hit.log"
if not defined TRIPWIRE set "TRIPWIRE=mesh-tripwire-hit.log"
>>"%TRIPWIRE%" echo %~f0 called with: %*
if defined _SUITE_TRIPWIRE_FILE if /i not "%_SUITE_TRIPWIRE_FILE%"=="%TRIPWIRE%" >>"%_SUITE_TRIPWIRE_FILE%" echo %~f0 called with: %*
exit /b 99
