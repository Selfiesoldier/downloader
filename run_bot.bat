@echo off
title Universal Telegram Downloader Bot
cd /d "%~dp0"
echo ========================================================
echo        STARTING TELEGRAM MEDIA DOWNLOADER BOT
echo ========================================================
".venv\Scripts\python.exe" bot.py
pause

