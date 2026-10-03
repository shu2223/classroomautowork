Option Explicit
Dim caFiles, caShell, caRepo, caPython, caScript, caQuote
Set caFiles = CreateObject("Scripting.FileSystemObject")
Set caShell = CreateObject("WScript.Shell")
caRepo = caFiles.GetParentFolderName(caFiles.GetParentFolderName(WScript.ScriptFullName))
caPython = caFiles.BuildPath(caRepo, ".venv\Scripts\pythonw.exe")
caScript = caFiles.BuildPath(caRepo, "scripts\start-ui.py")
caQuote = Chr(34)
If Not caFiles.FileExists(caPython) Then
    MsgBox "Python environment missing. Please install project dependencies first.", vbCritical, "Classroom"
    WScript.Quit 1
End If
caShell.CurrentDirectory = caRepo
caShell.Run caQuote & caPython & caQuote & " " & caQuote & caScript & caQuote, 0, False
