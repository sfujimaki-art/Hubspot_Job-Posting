# 専用PC に HR のタスク 2 本を登録する (2026-11-19 決定、手順書 .review/local-pc-cutover-2026-11-19.html)
#
#   JAS_HR_applicants : 5 分おき  pc_runner.py applicants (応募の登録)
#   JAS_HR_listings   : 毎日 01:01 / 12:01 / 20:01  pc_runner.py listings (求人の監視)
#                       (応募の 5 分おきの回が先に鍵を取れるよう 1 分ずらす)
#
# 前の回がまだ動いていれば次は始めない (MultipleInstances IgnoreNew。pc_runner.py の鍵と二重)。
# 既定は「無効」で登録する (-Enable を付けたときだけ有効)。切り替えの手順で有効にする。
# 管理者の PowerShell で、リポのフォルダから:
#   powershell -ExecutionPolicy Bypass -File scripts\job_application_sync\pc\register_hr_tasks.ps1
param(
  [string]$RepoDir = "C:\jas_hr",
  [string]$Python = "",
  [switch]$Enable
)
$ErrorActionPreference = "Stop"
if (-not $Python) { $Python = Join-Path $RepoDir ".venv\Scripts\python.exe" }
if (-not (Test-Path $Python)) { throw "python not found: $Python" }
$runner = "scripts\job_application_sync\pc_runner.py"
if (-not (Test-Path (Join-Path $RepoDir $runner))) { throw "pc_runner.py not found under $RepoDir" }

# ログオンしていなくても動かす (パスワードは保存しない S4U)。AW のタスクと同じ人で登録する。
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited

function Register-One($name, $arg, $triggers, $limitMin) {
  $action = New-ScheduledTaskAction -Execute $Python -Argument "$runner $arg" -WorkingDirectory $RepoDir
  $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes $limitMin) -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
  Register-ScheduledTask -TaskName $name -TaskPath "\JAS\" -Action $action -Trigger $triggers `
    -Settings $settings -Principal $principal -Force | Out-Null
  if ($Enable) { Enable-ScheduledTask -TaskName $name -TaskPath "\JAS\" | Out-Null }
  else { Disable-ScheduledTask -TaskName $name -TaskPath "\JAS\" | Out-Null }
  Write-Host ("registered \JAS\{0} (enabled={1})" -f $name, [bool]$Enable)
}

# 5 分おき (10 年続く繰り返し)
$every5 = New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
  -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
# 上限 35 分 = 子の処理 25 分 + 記録・余裕
Register-One "JAS_HR_applicants" "applicants" @($every5) 35

$t1 = New-ScheduledTaskTrigger -Daily -At "01:01"
$t2 = New-ScheduledTaskTrigger -Daily -At "12:01"
$t3 = New-ScheduledTaskTrigger -Daily -At "20:01"
# 上限 150 分 = 応募の回を待つ最大 15 分 + 子の処理 100 分 + 記録・余裕
Register-One "JAS_HR_listings" "listings" @($t1, $t2, $t3) 150
