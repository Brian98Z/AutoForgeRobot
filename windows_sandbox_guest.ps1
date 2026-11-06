$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$reqPath = 'C:\host\request.json'
$reportPath = 'C:\host\report.json'
$req = Get-Content -LiteralPath $reqPath -Raw | ConvertFrom-Json
if ($req.schema -ne 1 -or $req.run_id -notmatch '^[0-9a-f]{32}$') { throw 'invalid guest request' }
$deadline = [DateTime]::UtcNow.AddSeconds([int]$req.timeout_seconds)
$source = Join-Path 'C:\candidate' ([string]$req.artifact_path.Replace('/', '\'))
$guestDir = Join-Path $env:TEMP ('autoforge-' + $req.run_id)
New-Item -ItemType Directory -Path $guestDir -Force | Out-Null
$exe = Join-Path $guestDir 'candidate.exe'
function Find-Target([System.Windows.Automation.AutomationElement]$base, [string]$target, [switch]$Optional) {
  if ([string]::IsNullOrWhiteSpace($target)) { throw 'automation target must be a non-empty exact AutomationId or Name' }
  $byId = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::AutomationIdProperty, $target)
  $found = $base.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $byId)
  if ($null -eq $found) {
    $byName = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty, $target)
    $found = $base.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $byName)
  }
  if ($null -eq $found -and -not $Optional) { throw ('UI Automation target not found: ' + $target) }
  return $found
}
$report = [ordered]@{
  schema = 1; run_id = [string]$req.run_id; target = [string]$req.target
  candidate_sha256 = [string]$req.candidate_sha256; artifact_sha256 = [string]$req.artifact_sha256
  build = 'NOT_PERFORMED'; install = $false; launch = $false; sandbox = $false; cleanup = $false
  scenarios = @(); limitations = @('Portable EXE only; source build was not performed.', 'Windows Sandbox networking and clipboard redirection were disabled.')
}
$process = $null
try {
  $actual = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant()
  if ($actual -ne [string]$req.artifact_sha256) { throw 'read-only candidate artifact hash mismatch' }
  Copy-Item -LiteralPath $source -Destination $exe
  if ((Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash.ToLowerInvariant() -ne [string]$req.artifact_sha256) { throw 'guest copy hash mismatch' }
  $report.install = $true
  $report.sandbox = $true
  $launchArgs = @($req.artifact_args | Where-Object { $null -ne $_ })
  if ($launchArgs.Count -gt 0) {
    $process = Start-Process -FilePath $exe -ArgumentList $launchArgs -PassThru
  } else {
    $process = Start-Process -FilePath $exe -PassThru
  }
  $report.launch = $true
  $root = [System.Windows.Automation.AutomationElement]::RootElement
  $window = $null
  while ([DateTime]::UtcNow -lt $deadline -and $null -eq $window) {
    if ($process.HasExited) { throw 'portable application exited before its UI appeared' }
    $pidCondition = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ProcessIdProperty, $process.Id)
    $window = $root.FindFirst([System.Windows.Automation.TreeScope]::Children, $pidCondition)
    if ($null -eq $window -or [string]$window.Current.Name -cne [string]$req.target) { $window = $null }
    if ($null -eq $window) { Start-Sleep -Milliseconds 150 }
  }
  if ($null -eq $window) { throw 'named target window did not appear before timeout' }
  foreach ($scenario in $req.scenarios) {
    foreach ($action in $scenario.actions) {
      if ([string]$action.type -eq 'wait') {
        Start-Sleep -Milliseconds ([Math]::Min(5000, [Math]::Max(0, [int]$action.value)))
        continue
      }
      $el = Find-Target $window ([string]$action.target)
      switch ([string]$action.type) {
        'click' {
          $invoke = $el.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
          $invoke.Invoke()
        }
        'type' {
          $valuePattern = $el.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
          $valuePattern.SetValue([string]$action.value)
        }
        'key' {
          $el.SetFocus()
          $shell = New-Object -ComObject WScript.Shell
          $shell.SendKeys([string]$action.value)
        }
        default { throw ('unsupported action: ' + [string]$action.type) }
      }
    }
    $assertions = @()
    foreach ($assertion in $scenario.assertions) {
      $ok = $false
      $observed = $null
      if ($assertion.kind -eq 'text') {
        $element = if ($assertion.target) { Find-Target $window ([string]$assertion.target) } else { $window }
        $observed = [string]$element.Current.Name
        $expected = [string]$assertion.value
        $ok = switch ([string]$assertion.operator) {
          'equals' { $observed -ceq $expected }
          'contains' { $observed.Contains($expected) }
          'not_contains' { -not $observed.Contains($expected) }
        }
      } elseif ($assertion.kind -eq 'element') {
        $found = $null -ne (Find-Target $window ([string]$assertion.target) -Optional)
        $observed = $found
        $ok = if ($assertion.operator -eq 'exists') { $found -eq [bool]$assertion.value } else { $found -ne [bool]$assertion.value }
      } elseif ($assertion.kind -eq 'state') {
        $element = Find-Target $window ([string]$assertion.target)
        if ($assertion.operator -eq 'enabled') { $observed = [bool]$element.Current.IsEnabled }
        else { $observed = [bool]$element.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern).Current.ToggleState -eq [System.Windows.Automation.ToggleState]::On }
        $ok = $observed -eq [bool]$assertion.value
      }
      $assertions += [ordered]@{ pass = [bool]$ok; observed = $observed; kind = [string]$assertion.kind }
    }
    $report.scenarios += [ordered]@{ name = [string]$scenario.name; pass = (@($assertions | Where-Object { -not $_.pass }).Count -eq 0); assertions = $assertions }
  }
} finally {
  if ($null -ne $process -and -not $process.HasExited) {
    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    $process.WaitForExit(5000) | Out-Null
  }
  if ($null -ne $process) { $report.cleanup = [bool]$process.HasExited }
  if (Test-Path -LiteralPath $guestDir) { Remove-Item -LiteralPath $guestDir -Recurse -Force }
}
if (-not $report.cleanup) { throw 'guest application process cleanup not confirmed' }
if (Test-Path -LiteralPath $reportPath) { throw 'guest report path already exists' }
$reportJson = $report | ConvertTo-Json -Depth 8 -Compress
[IO.File]::WriteAllText($reportPath, $reportJson, (New-Object System.Text.UTF8Encoding($false)))
shutdown.exe /s /t 0 /f | Out-Null
Start-Sleep -Seconds 3
