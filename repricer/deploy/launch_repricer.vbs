' Repricer launcher for the desktop shortcut.
' Runs deploy\run_repricer.ps1 with NO window at all: powershell started
' directly from a shortcut flashes a blue console for a second on every click,
' even with -WindowStyle Hidden. ASCII only on purpose: wscript reads .vbs in
' the ANSI code page, and the path is built from this file's own folder.
Dim shell, fso, here, port
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
port = "8002"
If WScript.Arguments.Count > 0 Then port = WScript.Arguments(0)
shell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & _
    here & "\run_repricer.ps1"" -Port " & port, 0, False
