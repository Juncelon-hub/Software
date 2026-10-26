@echo off
rem Message Monitor 一键启动
cd /d %~dp0
.venv\Scripts\python main.py
if errorlevel 1 pause
