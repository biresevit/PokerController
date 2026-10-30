param(
 [Parameter(Mandatory=$true)][string]$ConfigPath,
 [string]$ControllerRoot,
 [string]$WorkerOutput,
 [string]$DecisionPath,
 [switch]$FreshInstall,
 [string]$DecisionSettingsPath,
 [string]$WorkerSettingsPath,
 [switch]$NoScheduledTask
)
$ErrorActionPreference='Stop'
$cfg=Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
if(-not $cfg.Token -or -not $cfg.TrustedKeys -or $cfg.MachineId -lt 1){throw 'Invalid updater enrollment'}
$root=[IO.Path]::GetFullPath($cfg.Root)
$agentRoot=Join-Path (Split-Path $root -Parent) 'Updater'
if(Test-Path -LiteralPath (Join-Path $root 'operation.json')){throw 'An update operation needs recovery; do not replace the agent'}
$existingState=Test-Path -LiteralPath (Join-Path $root 'active.json')
if(Get-Process -Name PokerSuite.Updater -ErrorAction SilentlyContinue){throw 'Stop the existing updater before replacing it; do not stop it during an update'}
if($FreshInstall){
 if($existingState){throw 'Fresh install cannot replace an existing managed installation'}
 if(Get-Process -Name PDecision,PokerController -ErrorAction SilentlyContinue){throw 'Applications are already running; use existing installation enrollment'}
 if(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'poker_controller\.worker' }){throw 'An existing worker is running; use existing installation enrollment'}
 if(-not $DecisionSettingsPath -or -not $WorkerSettingsPath){throw 'Fresh install requires -DecisionSettingsPath and -WorkerSettingsPath from your administrator'}
 $decisionSettings=Get-Content -LiteralPath $DecisionSettingsPath -Raw | ConvertFrom-Json
 $workerSettings=Get-Content -LiteralPath $WorkerSettingsPath -Raw | ConvertFrom-Json
 if(-not $decisionSettings.Database.ConnectionString){throw 'PDecision Database.ConnectionString is required'}
 if(-not $workerSettings.SCHEDULE_SQL_CONNECTION_STRING -and -not $workerSettings.SqlConnectionString){throw 'Worker SQL connection setting is required'}
 foreach($property in $workerSettings.PSObject.Properties.Name){if($property -notin @('SCHEDULE_SQL_CONNECTION_STRING','SqlConnectionString','SCHEDULE_ODBC_DRIVER')){throw 'Unsupported worker runtime setting'}}
}elseif(-not $existingState){
$decision=Get-Process -Name PDecision -ErrorAction SilentlyContinue | Select-Object -First 1
if(-not $DecisionPath){if($decision){$DecisionPath=$decision.Path}else{throw 'Start PDecision or provide -DecisionPath'}}
if(-not (Test-Path -LiteralPath $DecisionPath)){throw 'PDecision executable not found'}
$workers=@(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'poker_controller\.worker' -and $_.Name -eq 'python.exe' })
if(-not $workers){throw 'Start the existing PokerController worker before installing the updater'}
$worker=$workers | Where-Object { $_.ParentProcessId -in $workers.ProcessId } | Select-Object -First 1
if(-not $worker){$worker=$workers | Select-Object -First 1}
if(-not $ControllerRoot){$candidate=Split-Path $PSScriptRoot -Parent;if(Test-Path -LiteralPath (Join-Path $candidate 'poker_controller\worker.py')){$ControllerRoot=$candidate}else{throw 'Provide -ControllerRoot pointing to the existing PokerController project'}}
$ControllerRoot=[IO.Path]::GetFullPath($ControllerRoot)
$python=Join-Path $ControllerRoot '.venv\Scripts\python.exe'
if(-not (Test-Path -LiteralPath $python)){throw 'Existing PokerController virtual environment was not found'}
if(-not $WorkerOutput){
 if($worker.CommandLine -match '--output\s+(?:"([^"]+)"|(\S+))'){$WorkerOutput=if($Matches[1]){$Matches[1]}else{$Matches[2]}}else{$WorkerOutput='artifacts\azure-worker'}
}
if(-not [IO.Path]::IsPathRooted($WorkerOutput)){$WorkerOutput=Join-Path $ControllerRoot $WorkerOutput}
if(-not (Test-Path -LiteralPath $WorkerOutput)){throw 'Current worker output directory not found'}
}
New-Item -ItemType Directory -Force -Path $root,$agentRoot,(Join-Path $root 'config') | Out-Null
$user=[Security.Principal.WindowsIdentity]::GetCurrent().Name
icacls $root /inheritance:r /grant:r "${user}:(OI)(CI)F" 'SYSTEM:(OI)(CI)F' | Out-Null
Get-ChildItem -LiteralPath $root -Force | ForEach-Object { icacls $_.FullName /inheritance:e /t | Out-Null }
icacls $agentRoot /inheritance:r /grant:r "${user}:(OI)(CI)F" 'SYSTEM:(OI)(CI)F' | Out-Null
icacls (Join-Path $root 'config') /inheritance:r /grant:r "${user}:(OI)(CI)F" 'SYSTEM:(OI)(CI)F' | Out-Null
$source=if(Test-Path -LiteralPath (Join-Path $PSScriptRoot 'agent')){Join-Path $PSScriptRoot 'agent'}else{Join-Path (Split-Path $PSScriptRoot -Parent) 'artifacts\updater-agent'}
Get-ChildItem -LiteralPath $source -File | Copy-Item -Destination $agentRoot -Force
Copy-Item -LiteralPath $ConfigPath -Destination (Join-Path $agentRoot 'agent.json') -Force
if($FreshInstall){
 Copy-Item -LiteralPath $DecisionSettingsPath -Destination (Join-Path $root 'config\pdecision.json') -Force
 Copy-Item -LiteralPath $WorkerSettingsPath -Destination (Join-Path $root 'config\worker-env.json') -Force
}elseif(-not $existingState){
 Copy-Item -LiteralPath (Join-Path (Split-Path $DecisionPath -Parent) 'appsettings.json') -Destination (Join-Path $root 'config\pdecision.json') -Force
 # Persist only the SQL settings needed by the worker, not the user's entire environment.
 $environment=@{}
 foreach($key in @('SCHEDULE_SQL_CONNECTION_STRING','SqlConnectionString','SCHEDULE_ODBC_DRIVER')){$value=[Environment]::GetEnvironmentVariable($key);if($value){$environment[$key]=$value}}
 if($environment.Count){$environment | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $root 'config\worker-env.json') -Encoding UTF8}
}
if(-not $FreshInstall -and -not $existingState){
 $workerProcess=Get-Process -Id $worker.ProcessId
 $state=@{DecisionExe=$DecisionPath;DecisionCwd=(Split-Path $DecisionPath -Parent);DecisionPid=if($decision){$decision.Id}else{0};DecisionStarted=if($decision){$decision.StartTime.ToUniversalTime().Ticks}else{0};WorkerExe=$python;WorkerProcessExe=$workerProcess.Path;WorkerCwd=$ControllerRoot;WorkerArgs=@('-m','poker_controller.worker','--site-id',"$($cfg.SiteId)",'--machine-id',"$($cfg.MachineId)",'--output',$WorkerOutput);WorkerPid=$workerProcess.Id;WorkerStarted=$workerProcess.StartTime.ToUniversalTime().Ticks;WorkerOutput=$WorkerOutput}
 $state | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $root 'active.json') -Encoding UTF8
}
$runtimePath=Join-Path $root 'config\worker-env.json'
if($WorkerSettingsPath){
 $runtime=Get-Content -LiteralPath $WorkerSettingsPath -Raw | ConvertFrom-Json
 if(-not $runtime.SCHEDULE_SQL_CONNECTION_STRING -and -not $runtime.SqlConnectionString){throw 'Worker SQL connection setting is required'}
 foreach($property in $runtime.PSObject.Properties.Name){if($property -notin @('SCHEDULE_SQL_CONNECTION_STRING','SqlConnectionString','SCHEDULE_ODBC_DRIVER')){throw 'Unsupported worker runtime setting'}}
 Copy-Item -LiteralPath $WorkerSettingsPath -Destination $runtimePath -Force
}elseif(-not (Test-Path -LiteralPath $runtimePath)){
 $environment=@{}
 foreach($key in @('SCHEDULE_SQL_CONNECTION_STRING','SqlConnectionString','SCHEDULE_ODBC_DRIVER')){$value=[Environment]::GetEnvironmentVariable($key);if($value){$environment[$key]=$value}}
 if(-not $environment.SCHEDULE_SQL_CONNECTION_STRING -and -not $environment.SqlConnectionString){throw 'Provide -WorkerSettingsPath with worker SQL settings for automatic startup after logon'}
 $environment | ConvertTo-Json | Set-Content -LiteralPath $runtimePath -Encoding UTF8
}
$exe=Join-Path $agentRoot 'PokerSuite.Updater.exe'
$installedConfig=Join-Path $agentRoot 'agent.json'
if(-not $NoScheduledTask){
 $action=New-ScheduledTaskAction -Execute $exe -Argument ('--config "'+$installedConfig+'"') -WorkingDirectory $agentRoot
 $trigger=New-ScheduledTaskTrigger -AtLogOn -User $user
 $principal=New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
 $settings=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
 Register-ScheduledTask -TaskName "PokerSuite Updater $($cfg.MachineId)" -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
}
Start-Process -FilePath $exe -ArgumentList @('--config',('"'+$installedConfig+'"')) -WorkingDirectory $agentRoot -WindowStyle Hidden
Write-Output "Updater installed for machine $($cfg.MachineId). Use the Updates page to schedule the first installation or a rolling update."
