@echo off
title Polymarket Copytrading Bot
echo ========================================================
echo       POLYMARKET COPYTRADING BOT - INICIALIZADOR
echo ========================================================
echo.

:: 1. Verifica se o Python esta instalado
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERRO] Python nao foi encontrado no seu computador!
    echo.
    echo Como resolver:
    echo 1. Acesse https://www.python.org/downloads/
    echo 2. Baixe o instalador do Python
    echo 3. ATENCAO: Marque a caixinha "Add Python to PATH" antes de clicar em Install!
    echo.
    pause
    exit /b 1
)

:: 2. Verifica se o ambiente virtual existe, senao cria
if not exist ".venv" (
    echo [1/4] Criando ambiente virtual Python (.venv)...
    python -m venv .venv
    if %errorlevel% neq 0 (
        echo [ERRO] Falha ao criar ambiente virtual.
        pause
        exit /b 1
    )
)

:: 3. Ativa o ambiente virtual
call .venv\Scripts\activate

:: 4. Instala ou atualiza dependencias
echo [2/4] Verificando e instalando bibliotecas...
pip install -r requirements.txt --quiet

:: 5. Cria o arquivo .env se nao existir
if not exist ".env" (
    echo [3/4] Criando arquivo .env inicial a partir de .env.example...
    copy .env.example .env >nul
)

:: 6. Verifica se o Bullpen CLI esta instalado
echo [4/4] Verificando Bullpen CLI (Conexao Polymarket)...
where bullpen >nul 2>&1
if %errorlevel% neq 0 (
    if not exist "%USERPROFILE%\.bullpen\bin\bullpen.exe" (
        echo.
        echo [AVISO] Bullpen CLI nao foi encontrado no seu computador.
        echo Instalando Bullpen CLI automaticamente para voce...
        powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://cli.bullpen.fi/install.ps1 | iex"
    )
)

echo.
echo ========================================================
echo [OK] Tudo pronto! Iniciando o Dashboard Web...
echo ========================================================
echo.
echo O painel visual sera aberto no seu navegador em:
echo http://localhost:5000
echo.
echo Para fechar o bot, feche esta janela ou aperte Ctrl + C.
echo ========================================================
echo.

:: Abre o navegador automaticamente apos 2 segundos em segundo plano
start "" cmd /c "timeout /t 2 /nobreak >nul && start http://localhost:5000"

:: Executa o dashboard
python bot.py dashboard
pause
