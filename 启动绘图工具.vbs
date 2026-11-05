Option Explicit
Dim shell, fso, folder, pythonw, script
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
pythonw = "C:\Python313\pythonw.exe"
script = fso.BuildPath(folder, "app\drawing_app.pyw")
shell.CurrentDirectory = folder
If Not fso.FileExists(pythonw) Then pythonw = "pythonw.exe"
shell.Run Chr(34) & pythonw & Chr(34) & " " & Chr(34) & script & Chr(34), 0, True
