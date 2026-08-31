foreach ($pid_ in @(17968, 13612)) {
    $p = Get-CimInstance Win32_Process -Filter "ProcessId = $pid_"
    if ($p) { Write-Host "PID=$pid_ CMD=$($p.CommandLine)" } else { Write-Host "PID=$pid_ NOT FOUND" }
}
