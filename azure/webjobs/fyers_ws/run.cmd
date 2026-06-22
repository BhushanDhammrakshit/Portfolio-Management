@echo off
REM Azure Continuous WebJob entry point.
REM Deploy this directory to: App_Data\jobs\continuous\fyers_ws\
REM    run.cmd
REM    settings.job
REM    run_fyers_ws.py (copy from /scripts)
REM
REM The WebJob runs under the same App Service plan as the Flask app
REM with no extra cost. settings.job marks it singleton so only ONE
REM instance runs even when the App Service scales out.

cd /d "%HOME%\site\wwwroot"
python scripts\run_fyers_ws.py
