# Registra no Agendador de Tarefas do Windows o refill local da fila de copies (Claude Code).
# Roda a cada 4 horas e 5 minutos após o logon. Se o PC estava desligado no horário, roda assim que ligar.
# O script só chama o Claude quando a fila está abaixo de REFILL_MIN_DAYS (padrão 5 dias) ou há produto novo.
#
# Uso (PowerShell, na pasta do projeto):
#   powershell -ExecutionPolicy Bypass -File scripts\setup_windows_task.ps1
# Remover:
#   Unregister-ScheduledTask -TaskName "PinterestBot-Refill" -Confirm:$false

$ErrorActionPreference = "Stop"
$TaskName = "PinterestBot-Refill"
$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

$python = (Get-Command python -ErrorAction Stop).Source
$pythonw = Join-Path (Split-Path $python) "pythonw.exe"   # sem janela de console piscando
if (Test-Path $pythonw) { $python = $pythonw }

$action = New-ScheduledTaskAction -Execute $python -Argument "scripts\local_refill.py" -WorkingDirectory $ProjectDir

$every4h = New-ScheduledTaskTrigger -Daily -At "00:30"
$every4h.Repetition = (New-ScheduledTaskTrigger -Once -At "00:30" `
    -RepetitionInterval (New-TimeSpan -Hours 4) -RepetitionDuration (New-TimeSpan -Days 1)).Repetition
$atLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$atLogon.Delay = "PT5M"

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

# Roda como o seu usuário (precisa do login do Claude Code e das credenciais do Git do seu perfil)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($every4h, $atLogon) `
    -Settings $settings -Principal $principal -Force `
    -Description "Gera a fila de copies do Pinterest Bot com Claude Code quando estiver acabando." | Out-Null

Write-Host "Tarefa '$TaskName' registrada."
Write-Host "  Python:  $python"
Write-Host "  Projeto: $ProjectDir"
Write-Host "  Log:     $ProjectDir\logs\refill.log"
Write-Host "Testar agora: Start-ScheduledTask -TaskName $TaskName"
