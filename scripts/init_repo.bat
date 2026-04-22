@echo off
REM
REM Agents Repository Initialization Script (Windows)
REM ==================================================
REM This script sets up the development environment after cloning.
REM
REM Usage:
REM   scripts\init_repo.bat [--skip-env] [--skip-index] [--skip-mcp]
REM
REM Flags:
REM   --skip-env     Skip .env file creation (useful if already configured)
REM   --skip-index   Skip embedding model download and index pre-build
REM   --skip-mcp     Skip MCP environment detection and configuration
REM   --help         Show this help message

setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1

REM Where users should report unexpected script failures (see :fatal_exit at end).
REM Override via AGENTS_ISSUES_URL for divergent forks / GHE mirrors.
set "REPO_URL_ISSUES=https://github.com/WonderMr/Agents/issues"
if defined AGENTS_ISSUES_URL set "REPO_URL_ISSUES=%AGENTS_ISSUES_URL%"

REM ============== ANSI Colors ==============
REM Generate ESC character (0x1B) for ANSI codes (Windows 10+)
REM Using PowerShell to reliably emit the ESC char
for /f %%E in ('powershell -noprofile -command "[char]27"') do set "ESC=%%E"
set "RED=%ESC%[31m"
set "GREEN=%ESC%[32m"
set "YELLOW=%ESC%[33m"
set "BLUE=%ESC%[34m"
set "CYAN=%ESC%[36m"
set "NC=%ESC%[0m"

REM ============== Configuration ==============
set "SCRIPT_DIR=%~dp0"
REM Remove trailing backslash
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
for %%I in ("%SCRIPT_DIR%\..") do set "REPO_ROOT=%%~fI"
set "VENV_PATH=%REPO_ROOT%\.venv"
set "HELPERS=%SCRIPT_DIR%\_helpers"

REM ============== Parse Arguments ==============
set "SKIP_ENV=false"
set "SKIP_INDEX=false"
set "SKIP_MCP=false"

:parse_args
if "%~1"=="" goto :args_done
if /I "%~1"=="--skip-env"    set "SKIP_ENV=true"    & shift & goto :parse_args
if /I "%~1"=="--skip-index"  set "SKIP_INDEX=true"   & shift & goto :parse_args
if /I "%~1"=="--skip-mcp"    set "SKIP_MCP=true"     & shift & goto :parse_args
if /I "%~1"=="--help" goto :show_help
if /I "%~1"=="-h"     goto :show_help
shift
goto :parse_args

:show_help
echo Agents Repository Initialization Script (Windows)
echo(
echo Usage:
echo   scripts\init_repo.bat [--skip-env] [--skip-index] [--skip-mcp]
echo(
echo Flags:
echo   --skip-env     Skip .env file creation
echo   --skip-index   Skip embedding model download and index pre-build
echo   --skip-mcp     Skip MCP environment detection and configuration
echo   --help         Show this help message
exit /b 0

:args_done

REM ============== Pre-flight Checks & Python Selection ==============

echo(
echo %CYAN%===============================%NC%
echo %BLUE%  Pre-flight Checks%NC%
echo %CYAN%===============================%NC%

set "SELECTED_PYTHON="
set "PY_VER="
set "PYCHECK=%HELPERS%\check_version.py"

REM Try py launcher (preferred on Windows; `py -3` picks the highest installed 3.x)
REM Using goto to avoid nested parentheses issues in cmd.exe.
REM `check_version.py` exits non-zero below the minimum, which makes `for /f`
REM no-op silently — SELECTED_PYTHON stays undefined and we fall through.
for /f "delims=" %%A in ('py -3 "%PYCHECK%" 2^>nul') do (set "SELECTED_PYTHON=py -3" & set "PY_VER=%%A")
if defined SELECTED_PYTHON goto :python_found

REM Try plain python
for /f "delims=" %%A in ('python "%PYCHECK%" 2^>nul') do (set "SELECTED_PYTHON=python" & set "PY_VER=%%A")
if defined SELECTED_PYTHON goto :python_found

REM Try python3
for /f "delims=" %%A in ('python3 "%PYCHECK%" 2^>nul') do (set "SELECTED_PYTHON=python3" & set "PY_VER=%%A")
if defined SELECTED_PYTHON goto :python_found

echo   %RED%x%NC% No suitable Python version found
echo        Please install Python 3.10 or newer and ensure `py -3`, `python`,
echo        or `python3` is on PATH.
exit /b 1

:python_found
echo   %GREEN%+%NC% Found suitable Python: %SELECTED_PYTHON% (%PY_VER%)

REM ============== Environment Configuration ==============

echo(
echo %CYAN%===============================%NC%
echo %BLUE%  Environment Configuration%NC%
echo %CYAN%===============================%NC%

set "ENV_FILE=%REPO_ROOT%\.env"
set "ENV_EXAMPLE=%REPO_ROOT%\env.example"

if "%SKIP_ENV%"=="true" (
    echo   %GREEN%^>%NC% Skipping .env configuration --skip-env
    goto :env_done
)

if exist "%ENV_FILE%" goto :env_exists
if exist "%ENV_EXAMPLE%" goto :env_create
echo   %RED%x%NC% env.example not found
goto :env_done

:env_create
echo   %GREEN%^>%NC% Creating .env from env.example...
copy /Y "%ENV_EXAMPLE%" "%ENV_FILE%" >nul
if !errorlevel! neq 0 (
    set "_FATAL_EC=!errorlevel!"
    set "_FATAL_CTX=Failed to create .env from env.example (copy)"
    goto :fatal_exit
)
echo   %GREEN%+%NC% .env created successfully
echo(
echo   %YELLOW%Required configuration:%NC%
echo     * LANGFUSE_PUBLIC_KEY - LangFuse public key (optional)
echo     * LANGFUSE_SECRET_KEY - LangFuse secret key (optional)
echo     * ANTHROPIC_API_KEY   - For document OCR (optional)
echo(
goto :env_done

:env_exists
echo   %YELLOW%WARNING:%NC% .env file already exists
echo   %GREEN%^>%NC% Checking for missing keys...
%SELECTED_PYTHON% "%HELPERS%\merge_env.py" "%ENV_FILE%" "%ENV_EXAMPLE%"

:env_done

REM ============== Virtual Environment & Dependencies ==============

echo(
echo %CYAN%===============================%NC%
echo %BLUE%  Virtual Environment and Dependencies%NC%
echo %CYAN%===============================%NC%

set "SKIP_INSTALL=false"

if exist "%VENV_PATH%\Scripts\python.exe" goto :venv_exists
echo   %GREEN%^>%NC% Creating virtual environment using %SELECTED_PYTHON%...
%SELECTED_PYTHON% -m venv "%VENV_PATH%"
goto :venv_activate

:venv_exists
set "VENV_PYTHON=%VENV_PATH%\Scripts\python.exe"
set "VENV_PY_VER="
for /f "delims=" %%A in ('"%VENV_PYTHON%" "%PYCHECK%" 2^>nul') do set "VENV_PY_VER=%%A"
if not defined VENV_PY_VER (
    REM check_version.py failed — get version directly for display
    for /f "delims=" %%A in ('"%VENV_PYTHON%" -c "import sys;v=sys.version_info;print(str(v.major)+'.'+str(v.minor))" 2^>nul') do set "VENV_PY_VER=%%A"
    echo   %YELLOW%WARNING:%NC% Venv Python !VENV_PY_VER! may be unsupported
)
echo   %GREEN%+%NC% Virtual environment exists (!VENV_PY_VER!)

REM Compare venv Python version with selected interpreter
for /f "delims=" %%A in ('%SELECTED_PYTHON% -c "import sys;v=sys.version_info;print(str(v.major)+'.'+str(v.minor))" 2^>nul') do set "SELECTED_PY_SHORT=%%A"
if not "!VENV_PY_VER!"=="!SELECTED_PY_SHORT!" echo   %YELLOW%WARNING:%NC% Venv python version (!VENV_PY_VER!) differs from selected (!SELECTED_PY_SHORT!)

echo(
echo   %YELLOW%WARNING:%NC% Do you want to recreate it and reinstall all packages?
set "REPLY=N"
set /p "REPLY=  Reinstall? [y/N]: "
if /I not "!REPLY!"=="y" (
    echo   %GREEN%^>%NC% Using existing virtual environment
    set "SKIP_INSTALL=true"
    goto :venv_activate
)
echo   %GREEN%^>%NC% Removing existing venv...
rmdir /S /Q "%VENV_PATH%"
echo   %GREEN%^>%NC% Creating fresh virtual environment using %SELECTED_PYTHON%...
%SELECTED_PYTHON% -m venv "%VENV_PATH%"

:venv_activate
if not exist "%VENV_PATH%\Scripts\python.exe" (
    set "_FATAL_EC=1"
    set "_FATAL_CTX=Venv python not found after creation: %VENV_PATH%\Scripts\python.exe"
    goto :fatal_exit
)
echo   %GREEN%^>%NC% Activating virtual environment...
call "%VENV_PATH%\Scripts\activate.bat" 2>nul
if errorlevel 1 (
    echo   %YELLOW%WARNING:%NC% Venv activation failed — falling back to absolute paths
) else (
    echo   %GREEN%+%NC% Activated: %VENV_PATH%\Scripts\python.exe
)

if "!SKIP_INSTALL!"=="true" goto :skip_deps

echo(
echo %CYAN%===============================%NC%
echo %BLUE%  Installing Dependencies%NC%
echo %CYAN%===============================%NC%

echo   %GREEN%^>%NC% Upgrading pip...
"%VENV_PATH%\Scripts\python.exe" -m pip install --upgrade pip
if !errorlevel! neq 0 (
    set "_FATAL_EC=!errorlevel!"
    set "_FATAL_CTX=Failed to upgrade pip"
    goto :fatal_exit
)
echo(

echo   %GREEN%^>%NC% Installing requirements (this may take a few minutes)...
"%VENV_PATH%\Scripts\python.exe" -m pip install -r "%REPO_ROOT%\requirements.txt"
if !errorlevel! neq 0 (
    set "_FATAL_EC=!errorlevel!"
    set "_FATAL_CTX=Failed to install requirements (pip install -r requirements.txt)"
    goto :fatal_exit
)
echo(

echo   %GREEN%+%NC% All dependencies installed

:skip_deps

REM ============== Embedding Model Selection & Pre-indexing ==============

if "%SKIP_INDEX%"=="true" (
    echo(
    echo   %GREEN%^>%NC% Skipping pre-indexing --skip-index
    goto :index_done
)

echo(
echo %CYAN%===============================%NC%
echo %BLUE%  Embedding Model Selection and Pre-indexing%NC%
echo %CYAN%===============================%NC%

REM Check if model is already configured in .env (strip quotes, inline comments)
set "CURRENT_MODEL="
if exist "%ENV_FILE%" (
    for /f "tokens=1,* delims==" %%A in ('findstr /B /L "EMBEDDING_MODEL=" "%ENV_FILE%" 2^>nul') do set "CURRENT_MODEL=%%B"
)
REM Strip surrounding quotes and inline comments from CURRENT_MODEL
if defined CURRENT_MODEL (
    set "CURRENT_MODEL=!CURRENT_MODEL:"=!"
    for /f "tokens=1 delims=#" %%X in ("!CURRENT_MODEL!") do set "CURRENT_MODEL=%%X"
    REM Trim trailing spaces
    for /l %%i in (1,1,5) do if "!CURRENT_MODEL:~-1!"==" " set "CURRENT_MODEL=!CURRENT_MODEL:~0,-1!"
)

if defined CURRENT_MODEL (
    echo   %GREEN%+%NC% Embedding model already configured: !CURRENT_MODEL!
    goto :model_selected
)

echo(
echo   %CYAN%Select embedding model:%NC%
echo(
echo     %GREEN%1)%NC% Full     - intfloat/multilingual-e5-large                                    ~1.1 GB  1024d  multilingual
echo                Best quality. For powerful machines (32+ GB RAM).
echo(
echo     %GREEN%2)%NC% Balanced - sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2       ~120 MB  384d   multilingual
echo                Good quality, 9x lighter. For 16 GB machines. %GREEN%(Recommended)%NC%
echo(
echo     %GREEN%3)%NC% Light    - sentence-transformers/all-MiniLM-L6-v2                            ~22 MB   384d   English
echo                Minimal footprint. English queries only.
echo(

set "MODEL_CHOICE=2"
set /p "MODEL_CHOICE=  Choice [1/2/3] (default: 2): "

if "!MODEL_CHOICE!"=="1" (
    set "CURRENT_MODEL=intfloat/multilingual-e5-large"
) else if "!MODEL_CHOICE!"=="3" (
    set "CURRENT_MODEL=sentence-transformers/all-MiniLM-L6-v2"
) else (
    set "CURRENT_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
)

REM Write model to .env
set "_TMPPY=%TEMP%\agents_set_model_%RANDOM%.py"
(
    echo import os
    echo env_path = os.environ['ENV_FILE']
    echo new_model = os.environ['NEW_MODEL']
    echo lines = []
    echo if os.path.exists(env_path^):
    echo     with open(env_path, encoding='utf-8'^) as f:
    echo         lines = [l for l in f.readlines(^) if not l.startswith('EMBEDDING_MODEL='^)]
    echo lines.append(f'EMBEDDING_MODEL={new_model}\n'^)
    echo with open(env_path, 'w', encoding='utf-8'^) as f:
    echo     f.writelines(lines^)
) > "!_TMPPY!"
set "ENV_FILE=%ENV_FILE%"
set "NEW_MODEL=!CURRENT_MODEL!"
"%VENV_PATH%\Scripts\python.exe" "!_TMPPY!"
if !errorlevel! neq 0 (
    set "_FATAL_EC=!errorlevel!"
    set "_FATAL_CTX=Failed to write EMBEDDING_MODEL to .env"
    del /Q "!_TMPPY!" 2>nul
    goto :fatal_exit
)
del /Q "!_TMPPY!" 2>nul
echo   %GREEN%+%NC% Embedding model: !CURRENT_MODEL!

:model_selected

echo   %GREEN%^>%NC% Pre-downloading model and indexing skills/implants...
echo   %GREEN%^>%NC% (this may take a few minutes on first run)

set "EMBEDDING_MODEL=!CURRENT_MODEL!"
set "_TMPIDX=%TEMP%\agents_preindex_%RANDOM%.py"
(
    echo import sys, os
    echo sys.path.insert(0, os.environ['REPO_ROOT']^)
    echo os.environ.setdefault('EMBEDDING_MODEL', os.environ.get('EMBEDDING_MODEL', ''^)^)
    echo.
    echo from dotenv import load_dotenv
    echo load_dotenv(os.path.join(os.environ['REPO_ROOT'], '.env'^)^)
    echo.
    echo # 1. Download/cache the embedding model (with corrupt cache recovery^)
    echo from src.engine.embedder import embed_texts
    echo MAX_RETRIES = 2
    echo for attempt in range(MAX_RETRIES^):
    echo     try:
    echo         embed_texts(['warmup']^)
    echo         print('Embedding model ready', flush=True^)
    echo         break
    echo     except Exception as e:
    echo         if attempt ^< MAX_RETRIES - 1:
    echo             print(f'Model load failed: {e}', flush=True^)
    echo             print('Clearing corrupted model cache and retrying...', flush=True^)
    echo             from src.engine.embedder import clear_model_cache, reset_model
    echo             clear_model_cache(os.environ.get('EMBEDDING_MODEL', ''^)^)
    echo             reset_model(^)
    echo         else:
    echo             raise
    echo.
    echo # 2. Pre-index skills and implants
    echo print('Indexing skills...', flush=True^)
    echo from src.engine.skills import SkillRetriever
    echo sr = SkillRetriever(^)
    echo print(f'Skills indexed: {sr.store.count(^)} entries', flush=True^)
    echo.
    echo print('Indexing implants...', flush=True^)
    echo from src.engine.implants import ImplantRetriever
    echo ir = ImplantRetriever(^)
    echo print(f'Implants indexed: {ir.store.count(^)} entries', flush=True^)
) > "!_TMPIDX!"
set "REPO_ROOT=%REPO_ROOT%"
"%VENV_PATH%\Scripts\python.exe" "!_TMPIDX!"
if !errorlevel! equ 0 (
    echo   %GREEN%+%NC% Embedding model cached, skills and implants indexed
) else (
    echo   %YELLOW%WARNING:%NC% Pre-indexing failed - it will run on first MCP server start
)
del /Q "!_TMPIDX!" 2>nul

:index_done

REM ============== MCP Environment Detection & Configuration ==============

echo(
echo %CYAN%===============================%NC%
echo %BLUE%  MCP Environment Detection and Configuration%NC%
echo %CYAN%===============================%NC%

set "PYTHON_ABS=%VENV_PATH%\Scripts\python.exe"
set "SERVER_ABS=%REPO_ROOT%\src\server.py"

REM Count agents, skills, implants
set "AGENTS_BASE=%REPO_ROOT%\agents"
if not exist "%AGENTS_BASE%" (
    echo   %RED%x%NC% Agents directory not found
    goto :agents_count_done
)
echo   %GREEN%+%NC% Agents directory found: %AGENTS_BASE%
for /f %%N in ('"%PYTHON_ABS%" -c "import os,glob;p=os.environ.get('AGENTS_BASE','');print(len(glob.glob(os.path.join(p,'**','system_prompt.mdc'),recursive=True)))"') do echo     * %CYAN%%%N%NC% agents
for /f %%N in ('"%PYTHON_ABS%" -c "import os,glob;p=os.environ.get('REPO_ROOT','');print(len(glob.glob(os.path.join(p,'skills','*.mdc'))))"') do echo     * %CYAN%%%N%NC% skills
for /f %%N in ('"%PYTHON_ABS%" -c "import os,glob;p=os.environ.get('REPO_ROOT','');print(len(glob.glob(os.path.join(p,'implants','*.mdc'))))"') do echo     * %CYAN%%%N%NC% implants
:agents_count_done

if "%SKIP_MCP%"=="true" (
    echo   %GREEN%^>%NC% Skipping MCP configuration --skip-mcp
    goto :mcp_done
)

set "CONFIGURED_ENVS="

echo(
echo   %GREEN%^>%NC% Detecting IDE environments...
echo(

REM --- Detect Cursor ---
set "CURSOR_DETECTED=false"
set "CURSOR_DIR=%USERPROFILE%\.cursor"
if not exist "%CURSOR_DIR%" (
    echo   %GREEN%^>%NC% Cursor IDE not detected
    goto :detect_claude_desktop
)
set "CURSOR_DETECTED=true"
echo   %GREEN%+%NC% Cursor IDE detected

:detect_claude_desktop
REM --- Detect Claude Desktop ---
set "CLAUDE_DESKTOP_DETECTED=false"
set "CLAUDE_DESKTOP_CONFIG="
if not exist "%APPDATA%\Claude" (
    echo   %GREEN%^>%NC% Claude Desktop not detected
    goto :detect_claude_code
)
set "CLAUDE_DESKTOP_DETECTED=true"
set "CLAUDE_DESKTOP_CONFIG=%APPDATA%\Claude\claude_desktop_config.json"
echo   %GREEN%+%NC% Claude Desktop detected

:detect_claude_code
REM --- Detect Claude Code ---
set "CLAUDE_CODE_DETECTED=false"
REM Tracks whether the routing section was successfully injected into Claude Code's
REM global CLAUDE.md — gates the final fallback "LLM Instructions Block".
set "CLAUDE_MD_CONFIGURED=false"
where claude >nul 2>&1
if !errorlevel! equ 0 set "CLAUDE_CODE_DETECTED=true"
if exist "%USERPROFILE%\.claude.json" set "CLAUDE_CODE_DETECTED=true"
if exist "%USERPROFILE%\.claude" set "CLAUDE_CODE_DETECTED=true"

if "!CLAUDE_CODE_DETECTED!"=="true" (
    echo   %GREEN%+%NC% Claude Code detected
) else (
    echo   %GREEN%^>%NC% Claude Code not detected
)

echo(

REM Get timestamp for backups
for /f %%T in ('powershell -noprofile -command "Get-Date -UFormat '%%s'"') do set "BACKUP_TS=%%T"

REM --- Configure Cursor ---
if not "!CURSOR_DETECTED!"=="true" goto :skip_cursor
set "MCP_SETTINGS_FILE=%CURSOR_DIR%\mcp.json"
echo   %GREEN%^>%NC% Configuring Cursor MCP...
if not exist "%MCP_SETTINGS_FILE%" echo { "mcpServers": {} } > "%MCP_SETTINGS_FILE%"

REM Backup before modifying
copy /Y "%MCP_SETTINGS_FILE%" "%MCP_SETTINGS_FILE%.backup.%BACKUP_TS%" >nul 2>&1

"%PYTHON_ABS%" "%HELPERS%\inject_mcp.py" "%MCP_SETTINGS_FILE%" "%PYTHON_ABS%" "%SERVER_ABS%"
if !errorlevel! equ 0 (
    echo   %GREEN%+%NC% Agents-Core added to Cursor mcp.json
    set "CONFIGURED_ENVS=!CONFIGURED_ENVS! Cursor"
) else (
    echo   %RED%x%NC% Failed to update Cursor mcp.json
)
:skip_cursor

REM --- Configure Claude Desktop ---
if not "!CLAUDE_DESKTOP_DETECTED!"=="true" goto :skip_claude_desktop
echo   %GREEN%^>%NC% Configuring Claude Desktop MCP...
if not exist "!CLAUDE_DESKTOP_CONFIG!" echo {} > "!CLAUDE_DESKTOP_CONFIG!"

REM Backup before modifying
copy /Y "!CLAUDE_DESKTOP_CONFIG!" "!CLAUDE_DESKTOP_CONFIG!.backup.%BACKUP_TS%" >nul 2>&1

"%PYTHON_ABS%" "%HELPERS%\inject_mcp.py" "!CLAUDE_DESKTOP_CONFIG!" "%PYTHON_ABS%" "%SERVER_ABS%"
if !errorlevel! equ 0 (
    echo   %GREEN%+%NC% Agents-Core added to Claude Desktop config
    set "CONFIGURED_ENVS=!CONFIGURED_ENVS! Claude-Desktop"
) else (
    echo   %RED%x%NC% Failed to update Claude Desktop config
)
:skip_claude_desktop

REM --- Configure Claude Code ---
if not "!CLAUDE_CODE_DETECTED!"=="true" goto :skip_claude_code
set "CLAUDE_CODE_DIR=%USERPROFILE%\.claude"
set "CLAUDE_CODE_MCP=%USERPROFILE%\.claude.json"

if not exist "%CLAUDE_CODE_DIR%" mkdir "%CLAUDE_CODE_DIR%"

REM 1. MCP server in ~/.claude.json
echo   %GREEN%^>%NC% Configuring Claude Code MCP (%CLAUDE_CODE_MCP%)...
if not exist "%CLAUDE_CODE_MCP%" echo {} > "%CLAUDE_CODE_MCP%"

REM Backup before modifying
copy /Y "%CLAUDE_CODE_MCP%" "%CLAUDE_CODE_MCP%.backup.%BACKUP_TS%" >nul 2>&1

"%PYTHON_ABS%" "%HELPERS%\inject_mcp.py" "%CLAUDE_CODE_MCP%" "%PYTHON_ABS%" "%SERVER_ABS%"
if !errorlevel! equ 0 (
    echo   %GREEN%+%NC% Agents-Core added to Claude Code ~/.claude.json
    set "CONFIGURED_ENVS=!CONFIGURED_ENVS! Claude-Code"
) else (
    echo   %RED%x%NC% Failed to update Claude Code config
)

REM 2. Global CLAUDE.md with routing instructions
set "CLAUDE_CODE_MD=%CLAUDE_CODE_DIR%\CLAUDE.md"
set "CLAUDE_MD_SRC=%REPO_ROOT%\scripts\templates\routing-protocol-core.md"

echo(
echo   %CYAN%Agents-Core wants to add routing instructions to:%NC%
echo     %CLAUDE_CODE_MD%
echo(
REM Default to Y so an empty Enter (or an inherited env var) doesn't flip the
REM decision — set /p leaves the variable unchanged on empty input.
set "_ALLOW_MD=Y"
set /p "_ALLOW_MD=  Allow? [Y/n]: "
REM Accept any input starting with n/N as denial ("n", "no", "NO", "No", etc.).
if /i "!_ALLOW_MD:~0,1!"=="n" (
    echo   %YELLOW%WARNING:%NC% Skipped CLAUDE.md injection — instructions will be printed at the end
    goto :skip_claude_code
)

if not exist "%CLAUDE_MD_SRC%" (
    echo   %YELLOW%WARNING:%NC% Template not found at %CLAUDE_MD_SRC%, skipping
    goto :skip_claude_code
)

REM Backup CLAUDE.md before modifying (if it exists)
if exist "%CLAUDE_CODE_MD%" (
    copy /Y "%CLAUDE_CODE_MD%" "%CLAUDE_CODE_MD%.backup.%BACKUP_TS%" >nul 2>&1
    echo   %GREEN%^>%NC% Backup created: %CLAUDE_CODE_MD%.backup.%BACKUP_TS%
)

"%PYTHON_ABS%" "%HELPERS%\inject_claude_md.py" "%CLAUDE_CODE_MD%" "%CLAUDE_MD_SRC%"
if !errorlevel! equ 0 (
    echo   %GREEN%+%NC% Global CLAUDE.md configured
    set "CLAUDE_MD_CONFIGURED=true"
) else (
    echo   %RED%x%NC% Failed to configure CLAUDE.md
)
:skip_claude_code

REM --- MCP Summary ---
echo(
if defined CONFIGURED_ENVS (
    echo   %GREEN%+%NC% MCP configured for:%CONFIGURED_ENVS%
) else (
    echo   %YELLOW%WARNING:%NC% No IDE environments detected
    echo   %GREEN%^>%NC% You can configure MCP manually later
)

:mcp_done

REM ============== Final Summary ==============

echo(
echo %CYAN%===============================%NC%
echo %BLUE%  Initialization Complete%NC%
echo %CYAN%===============================%NC%
echo(
echo   %GREEN%Next steps:%NC%
echo(
echo   1. Configure API keys in .env (if you haven't yet):
echo      %CYAN%notepad %ENV_FILE%%NC%
echo(

if defined CONFIGURED_ENVS (
    echo !CONFIGURED_ENVS! | findstr /C:"Cursor" >nul 2>&1 && echo   2. Restart Cursor IDE to activate MCP servers && echo(
    echo !CONFIGURED_ENVS! | findstr /C:"Claude-Desktop" >nul 2>&1 && echo   3. Restart Claude Desktop to activate MCP servers && echo(
    echo !CONFIGURED_ENVS! | findstr /C:"Claude-Code" >nul 2>&1 && echo   4. Claude Code is configured globally - start it in any directory: && echo      %CYAN%claude%NC% && echo(
)

echo   Test with a command:
echo      %CYAN%/route%NC% - check available agents
echo(

REM ============== Health Check ==============
REM Check for missing, empty, or placeholder ANTHROPIC_API_KEY (strip quotes/comments)
set "_API_KEY_OK=false"
if exist "%ENV_FILE%" (
    for /f "tokens=1,* delims==" %%A in ('findstr /B /L "ANTHROPIC_API_KEY=" "%ENV_FILE%" 2^>nul') do (
        set "_API_VAL=%%B"
        if defined _API_VAL (
            REM Strip quotes and inline comments
            set "_API_VAL=!_API_VAL:"=!"
            for /f "tokens=1 delims=#" %%X in ("!_API_VAL!") do set "_API_VAL=%%X"
            for /l %%i in (1,1,5) do if "!_API_VAL:~-1!"==" " set "_API_VAL=!_API_VAL:~0,-1!"
            if defined _API_VAL if not "!_API_VAL!"=="sk-ant-..." set "_API_KEY_OK=true"
        )
    )
)
if "!_API_KEY_OK!"=="false" echo   %YELLOW%WARNING:%NC% ANTHROPIC_API_KEY not configured - document OCR will be unavailable

echo   To enable repository memory ^& history in a project:
echo      Run %CYAN%describe_repo()%NC% in your first Claude session inside that repo.
echo      It writes a compressed overview into the repo's own CLAUDE.md
echo      (managed section -- not the global %%USERPROFILE%%\.claude\CLAUDE.md^).
echo      If MCP sampling is unavailable it returns %CYAN%status="needs_summary"%NC%
echo      and you finalize the write with %CYAN%write_repo_summary(...)%NC%.
echo      History is appended to history.md each turn via log_interaction(...) (called by Claude per the routing protocol).
echo(

REM ============== LLM Instructions Block ==============
REM Printed only as a fallback — when the routing section could not be injected
REM into Claude Code's global CLAUDE.md (Claude Code not detected, consent denied,
REM template missing, or injection failed). When injection succeeded, the user
REM already has these instructions in place and does not need to paste them manually.
set "TEMPLATE_FILE=%REPO_ROOT%\scripts\templates\routing-protocol-core.md"
if not "!CLAUDE_MD_CONFIGURED!"=="true" if exist "%TEMPLATE_FILE%" (
    echo %CYAN%=========================================%NC%
    echo(
    echo   %GREEN%Add the following block to your LLM's instruction file%NC%
    echo   (CLAUDE.md for Claude, .cursorrules for Cursor, etc.^).
    echo   %YELLOW%Keep the BEGIN/END marker lines intact%NC% so a later script
    echo   run can replace the section instead of appending a duplicate.
    echo(
    echo %CYAN%-----------------------------------------%NC%
    REM Markers must match scripts\_helpers\inject_claude_md.py (MARKER_BEGIN/MARKER_END).
    echo # ^>^>^> Agents-Core Routing Protocol (managed by init_repo) ^>^>^>
    echo(
    type "%TEMPLATE_FILE%"
    echo(
    echo # ^<^<^< Agents-Core Routing Protocol (managed by init_repo) ^<^<^<
    echo %CYAN%-----------------------------------------%NC%
    echo(
)

echo %GREEN%Happy coding%NC%
echo(
exit /b 0

REM ============== Fatal Error Handler ==============
REM Reachable only via `goto :fatal_exit`. Normal completion falls through
REM `exit /b 0` above and never reaches this label.
:fatal_exit
if not defined _FATAL_EC  set "_FATAL_EC=1"
if not defined _FATAL_CTX set "_FATAL_CTX=unknown failure"
echo(
echo %RED%================================================================%NC%
echo %RED%  FATAL: init_repo.bat aborted unexpectedly%NC%
echo %RED%================================================================%NC%
echo(
echo   Exit code : !_FATAL_EC!
echo   Context   : !_FATAL_CTX!
echo(
echo   %CYAN%Please open an issue:%NC% %REPO_URL_ISSUES%/new
echo(
echo   Copy/paste into the issue form:
echo(
echo   -- Title --
echo   [init_repo.bat] !_FATAL_CTX! (exit !_FATAL_EC!)
echo(
echo   -- Body --
echo   ### Environment
for /f "tokens=*" %%V in ('ver') do echo   - Windows: %%V
if defined SELECTED_PYTHON echo   - Python: !SELECTED_PYTHON! ^(!PY_VER!^)
echo(
echo   ### Failure
echo   - Exit code: !_FATAL_EC!
echo   - Context: !_FATAL_CTX!
echo(
echo   ### How to reproduce
echo   ^<steps -- flags passed, context^>
echo(
echo   ### Logs
echo   ^<paste the last ~50 lines of output above^>
echo(
exit /b !_FATAL_EC!
