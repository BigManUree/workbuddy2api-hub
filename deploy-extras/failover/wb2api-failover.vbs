' Auto-start the WB2api model-failover proxy (no console window).
' Copy this file to the Windows Startup folder:
'   %AppData%\Microsoft\Windows\Start Menu\Programs\Startup
' Configuration is read from the .env file next to the Python script.
CreateObject("Wscript.Shell").Run "pythonw ""%USERPROFILE%\projects\wb2api-push\deploy-extras\failover\wb2api-model-failover.py""", 0, False
