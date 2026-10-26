param(
    [string]$Workspace = (Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'DailyArxiv'),
    [string]$TaskName = 'DailyArxiv Research Update'
)
$ErrorActionPreference = 'Stop'
$launch = Join-Path $PSScriptRoot 'Launch-DailyArxiv.ps1'
$codeDirectory = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
if (-not (Test-Path -LiteralPath (Join-Path $codeDirectory '.venv\Scripts\python.exe'))) {
    throw 'Install the locked Python dependencies before registering the task.'
}
New-Item -ItemType Directory -Force -Path $Workspace | Out-Null
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$command = [Security.SecurityElement]::Escape((Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'))
$arguments = [Security.SecurityElement]::Escape('-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $launch + '" -Workspace "' + $Workspace + '"')
$boundary = (Get-Date).Date.ToString('yyyy-MM-ddTHH:mm:ss')
$subscription = [Security.SecurityElement]::Escape('<QueryList><Query Id="0" Path="Microsoft-Windows-NetworkProfile/Operational"><Select Path="Microsoft-Windows-NetworkProfile/Operational">*[System[EventID=10000]]</Select></Query></QueryList>')
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing -and ($existing.Actions.Arguments -notlike '*Launch-DailyArxiv.ps1*')) {
    throw 'A different task already uses this name. Choose another TaskName.'
}
$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Run arXiv locally after each announcement; catch up after login or reconnection. Completed announcements are skipped.</Description></RegistrationInfo>
  <Triggers>
    <CalendarTrigger><Repetition><Interval>PT5M</Interval><Duration>P1D</Duration><StopAtDurationEnd>false</StopAtDurationEnd></Repetition><StartBoundary>$boundary</StartBoundary><Enabled>true</Enabled><ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger>
    <LogonTrigger><Enabled>true</Enabled><UserId>$sid</UserId><Delay>PT15S</Delay></LogonTrigger>
    <EventTrigger><Enabled>true</Enabled><Subscription>$subscription</Subscription><Delay>PT10S</Delay></EventTrigger>
  </Triggers>
  <Principals><Principal id="Author"><UserId>$sid</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate><StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand><Enabled>true</Enabled><Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle><WakeToRun>false</WakeToRun><ExecutionTimeLimit>PT55M</ExecutionTimeLimit><Priority>7</Priority>
  </Settings>
  <Actions Context="Author"><Exec><Command>$command</Command><Arguments>$arguments</Arguments></Exec></Actions>
</Task>
"@
Register-ScheduledTask -TaskName $TaskName -Xml $xml -Force | Out-Null
$xml | Set-Content -LiteralPath (Join-Path $Workspace 'scheduled-task.xml') -Encoding Unicode
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
Get-ScheduledTaskInfo -TaskName $TaskName | Select-Object NextRunTime, LastTaskResult
