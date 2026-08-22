@echo off
title Configurar Carteira Polymarket (Bullpen)
echo ========================================================
echo       CONFIGURACAO DA CARTEIRA POLYMARKET (BULLPEN)
echo ========================================================
echo.
echo Este assistente vai conectar sua carteira da Polymarket
echo e aprovar as permissoes necessarias para o bot operar.
echo.

:: Verifica se o Bullpen esta no PATH ou no diretorio padrao
set "BULLPEN_EXE=bullpen"
where bullpen >nul 2>&1
if %errorlevel% neq 0 (
    if exist "%USERPROFILE%\.bullpen\bin\bullpen.exe" (
        set "BULLPEN_EXE=%USERPROFILE%\.bullpen\bin\bullpen.exe"
    ) else (
        echo [INFO] Bullpen CLI nao encontrado. Instalando automaticamente...
        powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://cli.bullpen.fi/install.ps1 | iex"
        set "BULLPEN_EXE=%USERPROFILE%\.bullpen\bin\bullpen.exe"
    )
)

echo.
echo [1/2] Iniciando o assistente guiado da Polymarket...
echo (Seu navegador sera aberto para fazer o login da Polymarket)
echo.
"%BULLPEN_EXE%" setup

echo.
echo [2/2] Testando status da carteira...
"%BULLPEN_EXE%" polymarket preflight

echo.
echo ========================================================
echo [OK] Configuracao concluida com sucesso!
echo Agora voce ja pode abrir o arquivo "iniciar_dashboard.bat"
echo ========================================================
echo.
pause
