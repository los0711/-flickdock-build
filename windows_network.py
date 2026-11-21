"""User-invoked Windows app firewall repair; never runs during QA or startup."""
from pathlib import Path
import ctypes
from ctypes import wintypes
import subprocess
import sys


def repair_network_access():
    if sys.platform!='win32' or not getattr(sys,'frozen',False):
        return False
    script=Path(sys._MEIPASS)/'windows-network.ps1'
    if not script.is_file():return False
    powershell=Path(__import__('os').environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
    arguments=subprocess.list2cmdline(['-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass',
        '-File',str(script),'-InstallDirectory',str(Path(sys.executable).parent)])
    # runas lets Windows present the normal administrator consent dialog.
    # No credentials, shells built from file names, or global firewall changes.
    execute=ctypes.windll.shell32.ShellExecuteW
    execute.argtypes=[wintypes.HWND,wintypes.LPCWSTR,wintypes.LPCWSTR,wintypes.LPCWSTR,wintypes.LPCWSTR,ctypes.c_int]
    execute.restype=ctypes.c_void_p
    result=execute(None,'runas',str(powershell),arguments,None,0)
    return bool(result and result>32)
