@echo off
echo Stopping Crawler Backend (app.py)...
taskkill /F /IM pythonw.exe /FI "WINDOWTITLE eq C:\Users\Personal\OneDrive\Desktop\Crawler\app.py*" >nul 2>&1
taskkill /F /IM python.exe /FI "WINDOWTITLE eq C:\Users\Personal\OneDrive\Desktop\Crawler\app.py*" >nul 2>&1
for /f "tokens=5" %%a in ('netstat -aon ^| find "5002" ^| find "LISTENING"') do taskkill /F /PID %%a >nul 2>&1
echo Stopped successfully.
pause
