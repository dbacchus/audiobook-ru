@echo off
rem То же самое, но с консолью: видны ошибки и сообщения движка.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
python audiobook_app.py
pause
