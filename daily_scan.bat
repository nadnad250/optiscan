@echo off
rem Scan quotidien OptiScan — lancé par le Planificateur de tâches Windows
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
python -m scanner.daily_scan >> output\daily_scan.log 2>&1
