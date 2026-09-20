@echo off
rem Запуск приложения без окна консоли. Если что-то не стартует,
rem запустите run_debug.bat -- там видны сообщения об ошибках.
cd /d "%~dp0"
start "" pythonw audiobook_app.py
