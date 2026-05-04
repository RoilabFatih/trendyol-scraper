@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
title Trendyol Yerel Ajan

if not exist ".venv\Scripts\python.exe" (
    echo [setup] Sanal ortam olusturuluyor...
    py -3.12 -m venv .venv || goto :err
    ".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet || goto :err
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt --quiet || goto :err
)

if not exist "local_config.json" (
    echo HATA: local_config.json yok.
    echo local_config.example.json dosyasini local_config.json olarak kopyalayip duzenleyin.
    pause
    exit /b 1
)

echo Yerel ajan baslatiliyor. Bu pencereyi acik birakin.
echo Kapatmak icin Ctrl+C basin.
echo.
".venv\Scripts\python.exe" local_worker.py
echo.
echo Yerel ajan durdu.
pause
exit /b 0

:err
echo Kurulum sirasinda hata olustu.
pause
exit /b 1
