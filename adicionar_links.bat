@echo off
rem Dê dois cliques para colar os links dos produtos que você acabou de publicar nas lojas.
cd /d "%~dp0"
python scripts\add_links.py
echo.
pause
