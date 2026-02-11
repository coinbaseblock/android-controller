@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem ==== Usage: tlist.bat [target_dir] [skip_file]
rem Example:  tlist.bat . txt.txt > txt.txt

set "target=%~1"
if "%target%"=="" set "target=."
set "skipfile=%~f2"

echo =================================
echo   TREE STRUCTURE
echo   Target: %target%
echo =================================
echo.
rem ASCII tree to avoid garbled characters when redirecting to a file
tree "%target%" /f /a
echo.

echo =================================
echo   LIST FILES + SHOW CONTENTS
echo   Target: %target%
echo =================================
echo.

set "MAXSIZE=2000000"

rem ---- allowed text extensions ----
set "ALLOWEXTS=.ps1 .txt .md .json .yaml .yml .xml .html .htm .css .js .ts .go .py .bat .cmd .ps1 .ini .conf .toml .env .gitignore .dockerfile"

rem ---- blacklist extensions (won't show even if in allow list) ----
set "SKIPEXTS=.mod .sum .lock .bat"

for /r "%target%" %%F in (*.*) do (
    set "full=%%~fF"
    set "ext=%%~xF"
    set "size=%%~zF"

    rem skip output file if provided as arg2
    if /I "!full!"=="%skipfile%" (
        rem skip
    ) else (
        set "okext=0"
        call :isTextExt "!ext!" "%ALLOWEXTS%" okext
        set "skipext=0"
        call :isTextExt "!ext!" "%SKIPEXTS%" skipext

        if "!okext!"=="1" if "!skipext!"=="0" (
            echo ----------------------------------------
            echo FILE: %%F
            echo ----------------------------------------
            if !size! GEQ %MAXSIZE% (
                echo [skip] file size !size! bytes ^(>= %MAXSIZE%^)
            ) else (
                type "%%F"
            )
            echo.
        )
    )
)

echo.
echo ============ DONE ============
exit /b 0

:isTextExt
setlocal EnableDelayedExpansion
set "extchk=%~1"
set "list=%~2"
set "found=0"
for %%E in (%list%) do (
  if /I "%%E"=="!extchk!" set "found=1"
)
endlocal & set "%~3=%found%"
exit /b