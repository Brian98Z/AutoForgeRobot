param()

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

$requestPath = 'C:\host\request.json'
$launchPath = 'C:\host\launch.json'
$request = Get-Content -LiteralPath $requestPath -Raw | ConvertFrom-Json
$config = Get-Content -LiteralPath $launchPath -Raw | ConvertFrom-Json
$target = [string]$request.target
$sandboxDir = Join-Path $env:TEMP ('autoforge-actuator-' + [string]$request.run_id)
New-Item -ItemType Directory -Path $sandboxDir -Force | Out-Null
$exe = Join-Path 'C:\candidate' ([string]$config.path).Replace('/', '\')
$args = @($config.args | Where-Object { $null -ne $_ })
$process = $null

function Find-Control([System.Windows.Automation.AutomationElement]$base, [string]$target) {
    if ([string]::IsNullOrWhiteSpace($target)) { throw 'empty UIA target' }
    $byId = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::AutomationIdProperty, $target)
    $found = $base.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $byId)
    if ($null -eq $found) {
        $byName = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty, $target)
        $found = $base.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $byName)
    }
    if ($null -eq $found) { throw ('UIA target not found: ' + $target) }
    return $found
}

function Write-HostJson([string]$name, $value) {
    $path = Join-Path 'C:\host' $name
    [IO.File]::WriteAllText($path, ($value | ConvertTo-Json -Depth 8 -Compress), (New-Object System.Text.UTF8Encoding($false)))
}

try {
    if ($args.Count -gt 0) { $process = Start-Process -FilePath $exe -ArgumentList $args -PassThru }
    else { $process = Start-Process -FilePath $exe -PassThru }
    "started pid=$($process.Id) exe=$exe" | Set-Content -Encoding utf8 'C:\host\launch.log'
} catch {
    Write-HostJson 'actuator-error.json' @{ ok = $false; error = $_.Exception.ToString() }
    exit 1
}

$root = [System.Windows.Automation.AutomationElement]::RootElement
$window = $null
$deadline = [DateTime]::UtcNow.AddSeconds([int]$request.timeout_seconds)
while ([DateTime]::UtcNow -lt $deadline -and $null -eq $window) {
    if ($process.HasExited) { Write-HostJson 'actuator-error.json' @{ ok = $false; error = 'candidate exited before its window appeared' }; break }
    $condition = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ProcessIdProperty, $process.Id)
    $candidate = $root.FindFirst([System.Windows.Automation.TreeScope]::Children, $condition)
    if ($null -ne $candidate -and [string]$candidate.Current.Name -ceq $target) { $window = $candidate }
    if ($null -eq $window) { Start-Sleep -Milliseconds 150 }
}
if ($null -eq $window) {
    Write-HostJson 'actuator-error.json' @{ ok = $false; error = 'target window did not appear' }
} else {
    Write-HostJson 'ready.json' @{ ok = $true; pid = $process.Id; target = $target }
}

while ($null -ne $window -and -not (Test-Path -LiteralPath 'C:\host\stop')) {
    $actionPath = 'C:\host\action.json'
    if (Test-Path -LiteralPath $actionPath) {
        $message = Get-Content -LiteralPath $actionPath -Raw | ConvertFrom-Json
        $result = [ordered]@{ schema = 1; id = [string]$message.id; ok = $false; error = $null }
        try {
            $action = $message.action
            $kind = [string]$action.type
            if ($kind -eq 'type') {
                $control = Find-Control $window ([string]$action.target)
                $pattern = $control.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
                $pattern.SetValue([string]$action.value)
            } elseif ($kind -eq 'click') {
                $control = Find-Control $window ([string]$action.target)
                $pattern = $control.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
                $pattern.Invoke()
            } elseif ($kind -eq 'key') {
                $control = Find-Control $window ([string]$action.target)
                $control.SetFocus()
                $shell = New-Object -ComObject WScript.Shell
                $shell.SendKeys([string]$action.value)
            } elseif ($kind -eq 'wait') {
                Start-Sleep -Milliseconds ([Math]::Min(5000, [Math]::Max(0, [int]$action.value)))
            } else {
                throw ('unsupported action: ' + $kind)
            }
            $result.ok = $true
        } catch {
            $result.error = $_.Exception.ToString()
        }
        Write-HostJson 'acted.json' $result
        Remove-Item -LiteralPath $actionPath -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 100
}

if ($null -ne $process -and -not $process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue }
if (Test-Path -LiteralPath $sandboxDir) { Remove-Item -LiteralPath $sandboxDir -Recurse -Force -ErrorAction SilentlyContinue }
Write-HostJson 'stopped.json' @{ ok = $true }
shutdown.exe /s /t 0 /f | Out-Null
