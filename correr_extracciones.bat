@echo off
cd /d C:\Users\Marco\OneDrive\Desktop\historias_automatizacion
C:\Users\Marco\anaconda3\envs\llm_extraction\python.exe DiagCheck\code_master.py >> DiagCheck\log_extraccion.txt 2>&1
echo exit code: %ERRORLEVEL% >> DiagCheck\log_extraccion.txt
echo. >> DiagCheck\log_extraccion.txt