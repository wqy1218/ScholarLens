$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut("$env:USERPROFILE\Desktop\ScholarLens.lnk")
$lnk.TargetPath = 'D:\ScholarLens\node_modules\electron\dist\electron.exe'
$lnk.Arguments = '.'
$lnk.WorkingDirectory = 'D:\ScholarLens'
$lnk.IconLocation = 'D:\ScholarLens\assets\icon.ico,0'
$lnk.Description = 'ScholarLens - paper reading workbench'
$lnk.Save()
Write-Output 'shortcut created'
