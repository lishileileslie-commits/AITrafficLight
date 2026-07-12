' 双击运行: 静默启动 AI 红绿灯 (不弹黑色控制台窗口)
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
' UpTime 计时器/备注已并入 app.py 同一个窗口, 不再单独启动 standup.py
sh.Run "pythonw.exe app.py", 0, False
