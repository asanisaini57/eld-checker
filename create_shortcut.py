import subprocess
import sys
import os

pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
if not os.path.exists(pythonw):
    pythonw = sys.executable

work_dir = os.path.dirname(os.path.abspath(__file__))
desktop = os.path.join(os.path.expanduser("~"), "Desktop")
lnk_path = os.path.join(desktop, "ELD Checker.lnk")

ps = (
    f'$WshShell = New-Object -ComObject WScript.Shell;'
    f'$s = $WshShell.CreateShortcut("{lnk_path}");'
    f'$s.TargetPath = "{pythonw}";'
    f'$s.Arguments = "launcher.py";'
    f'$s.WorkingDirectory = "{work_dir}";'
    f'$s.Description = "ELD Checker Server Launcher";'
    f'$s.Save()'
)

subprocess.run(["powershell", "-Command", ps], check=True)
print(f"Shortcut created: {lnk_path}")
