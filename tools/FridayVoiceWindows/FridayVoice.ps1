# Friday Voice: shows, on this laptop, that Friday's voice runs here and uses the GPU.
#
#   Tray icon   grey = voice server off, green = ready, pulsing green = speaking now,
#               red = the Mac can't reach this laptop (WSL's SSH server is down)
#   Overlay     a small card in the bottom-right corner: status, a live graph of GPU load, VRAM in use,
#               temperature and power, and how fast the last line was made (time to first audio, speed vs
#               real time). Tray menu: show it always, only while speaking, or never.
#
# While it runs it also keeps WSL awake and its SSH server up (WSL here has no systemd, so nothing else
# starts sshd, and the Mac can only start the voice server over SSH).
#
#   powershell -ExecutionPolicy Bypass -File FridayVoice.ps1              the tray app
#   powershell -ExecutionPolicy Bypass -File FridayVoice.ps1 -Snapshot x  draw the card once into x.png, exit
#
# Data: GET http://127.0.0.1:8091/stats from friday_tts_server.py (WSL forwards localhost), and nvidia-smi.

param([string]$Snapshot = "")

Add-Type -AssemblyName System.Windows.Forms, System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$Distro = 'Ubuntu'
$Port = 8091
$ServerCmd = 'mkdir -p ~/friday-tts/logs; curl -s -m 2 http://127.0.0.1:8091/health >/dev/null || setsid nohup ~/friday-tts/faster/.venv/bin/python ~/friday-tts/scripts/friday_tts_server.py --talker Q8_0 --codec BF16 --host 127.0.0.1 --port 8091 > ~/friday-tts/logs/server.log 2>&1 < /dev/null &'
$Dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PrefsFile = Join-Path $Dir 'prefs.json'

$Prefs = @{ overlay = 'always' }
if (Test-Path $PrefsFile) {
    try { $p = Get-Content $PrefsFile -Raw | ConvertFrom-Json; if ($p.overlay) { $Prefs.overlay = [string]$p.overlay } } catch {}
}
function Save-Prefs { try { $Prefs | ConvertTo-Json | Set-Content -Path $PrefsFile -Encoding UTF8 } catch {} }

$S = @{
    status = 'off'; stats = $null; gpu = $null; sshOk = $true; keeper = $null
    history = New-Object 'System.Collections.Generic.List[double]'; lastWsl = [datetime]::MinValue; pulse = $false
}

# --- colours and fonts -------------------------------------------------------------------------------
$C = @{
    bg = [System.Drawing.Color]::FromArgb(16, 18, 22); line = [System.Drawing.Color]::FromArgb(40, 44, 52)
    fg = [System.Drawing.Color]::FromArgb(236, 239, 244); dim = [System.Drawing.Color]::FromArgb(150, 158, 172)
    faint = [System.Drawing.Color]::FromArgb(105, 112, 125)
    ready = [System.Drawing.Color]::FromArgb(61, 214, 140); speak = [System.Drawing.Color]::FromArgb(140, 179, 255)
    off = [System.Drawing.Color]::FromArgb(120, 124, 132); bad = [System.Drawing.Color]::FromArgb(255, 90, 78)
}
$F = @{
    title = New-Object System.Drawing.Font('Segoe UI Semibold', 10)
    body = New-Object System.Drawing.Font('Segoe UI', 9)
    small = New-Object System.Drawing.Font('Segoe UI', 8)
    tag = New-Object System.Drawing.Font('Segoe UI Semibold', 8)
}

# --- data ------------------------------------------------------------------------------------------
function Get-Stats {
    try { return Invoke-RestMethod -Uri "http://127.0.0.1:$Port/stats" -TimeoutSec 1 } catch { return $null }
}

function Get-Gpu {
    try {
        $line = & nvidia-smi.exe --query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw --format=csv,noheader,nounits 2>$null |
            Select-Object -First 1
        if (-not $line) { return $null }
        $f = $line -split ',\s*'
        $name = ($f[0] -replace 'NVIDIA GeForce ', '' -replace ' Laptop GPU', '').Trim()
        $watts = 0.0
        $power = if ([double]::TryParse($f[5], [ref]$watts)) { $watts.ToString('0') } else { '?' }
        return @{ name = $name; util = [double]$f[1]; used = [double]$f[2]; total = [double]$f[3]; temp = [double]$f[4]; power = $power }
    } catch { return $null }
}

function Ensure-Wsl {
    # Keep WSL running (it shuts down when nothing holds it, taking SSH with it) and its SSH server up.
    if (-not $S.keeper -or $S.keeper.HasExited) {
        $psi = New-Object System.Diagnostics.ProcessStartInfo 'wsl.exe', "-d $Distro -e sleep infinity"
        $psi.CreateNoWindow = $true; $psi.UseShellExecute = $false
        try { $S.keeper = [System.Diagnostics.Process]::Start($psi) } catch {}
    }
    $out = & wsl.exe -d $Distro -u root -e sh -c 'pgrep -x sshd >/dev/null || /usr/sbin/service ssh start >/dev/null 2>&1; pgrep -x sshd >/dev/null && echo up' 2>$null
    $S.sshOk = [bool]($out -match 'up')
}

function Update-State {
    $S.stats = Get-Stats
    $S.gpu = Get-Gpu
    if ($S.gpu) {
        $S.history.Add($S.gpu.util)
        while ($S.history.Count -gt 60) { $S.history.RemoveAt(0) }
    }
    if (((Get-Date) - $S.lastWsl).TotalSeconds -ge 30) { $S.lastWsl = Get-Date; Ensure-Wsl }
    if (-not $S.sshOk) { $S.status = 'unreachable' }
    elseif (-not $S.stats) { $S.status = 'off' }
    elseif ($S.stats.generating) { $S.status = 'speaking' }
    else { $S.status = 'ready' }
    $S.pulse = -not $S.pulse
}

function Status-Text {
    switch ($S.status) {
        'speaking' { 'Speaking' }
        'ready' { 'Ready' }
        'off' { 'Voice server off' }
        'unreachable' { 'Mac can''t reach WSL' }
    }
}

function Status-Color {
    switch ($S.status) {
        'speaking' { $C.speak } 'ready' { $C.ready } 'off' { $C.off } 'unreachable' { $C.bad }
    }
}

# --- drawing the card -----------------------------------------------------------------------------
$CardW = 340; $CardH = 118

function Draw-Card([System.Drawing.Graphics]$g) {
    $g.SmoothingMode = 'AntiAlias'; $g.TextRenderingHint = 'ClearTypeGridFit'
    $g.Clear($C.bg)
    $col = Status-Color
    # title row
    $dot = if ($S.status -eq 'speaking' -and $S.pulse) { 12 } else { 10 }
    $g.FillEllipse((New-Object System.Drawing.SolidBrush $col), 14 + (12 - $dot) / 2, 15 + (12 - $dot) / 2, $dot, $dot)
    $g.DrawString('Friday voice', $F.title, (New-Object System.Drawing.SolidBrush $C.fg), 32, 10)
    $st = Status-Text
    $sz = $g.MeasureString($st, $F.tag)
    $g.DrawString($st, $F.tag, (New-Object System.Drawing.SolidBrush $col), $CardW - 14 - $sz.Width, 13)
    # GPU line
    $gpuText = 'GPU not visible (nvidia-smi)'
    if ($S.gpu) {
        $gpuText = '{0}  ·  GPU {1}%  ·  {2:N1} / {3:N1} GB  ·  {4}°C  ·  {5} W' -f $S.gpu.name, [int]$S.gpu.util,
            ($S.gpu.used / 1024), ($S.gpu.total / 1024), [int]$S.gpu.temp, $S.gpu.power
    }
    $g.DrawString($gpuText, $F.body, (New-Object System.Drawing.SolidBrush $C.dim), 14, 34)
    # GPU load graph (last 60 s)
    $gx = 14; $gy = 56; $gw = $CardW - 28; $gh = 30
    $g.DrawRectangle((New-Object System.Drawing.Pen $C.line), $gx, $gy, $gw, $gh)
    $n = $S.history.Count
    if ($n -ge 2) {
        $pts = New-Object 'System.Collections.Generic.List[System.Drawing.PointF]'
        $pts.Add((New-Object System.Drawing.PointF ($gx + $gw - ($n - 1) * $gw / 59), ($gy + $gh)))
        for ($i = 0; $i -lt $n; $i++) {
            $x = $gx + $gw - ($n - 1 - $i) * $gw / 59
            $y = $gy + $gh - [Math]::Min(100, $S.history[$i]) / 100 * ($gh - 2) - 1
            $pts.Add((New-Object System.Drawing.PointF $x, $y))
        }
        $pts.Add((New-Object System.Drawing.PointF ($gx + $gw), ($gy + $gh)))
        $fill = [System.Drawing.Color]::FromArgb(70, $C.speak.R, $C.speak.G, $C.speak.B)
        $g.FillPolygon((New-Object System.Drawing.SolidBrush $fill), $pts.ToArray())
        $line = $pts.GetRange(1, $n).ToArray()
        $g.DrawLines((New-Object System.Drawing.Pen $C.speak, 1.6), $line)
    }
    $g.DrawString('GPU load, last minute', $F.small, (New-Object System.Drawing.SolidBrush $C.faint), $gx + 3, $gy + 1)
    # last line
    $foot = 'Waiting for the first line from the Mac'
    if (-not $S.stats) { $foot = 'Start it from the Mac (open FridayEars) or the tray menu' }
    elseif ($S.stats.requests -gt 0 -and $S.stats.last_rtf) {
        $speed = 1.0 / [double]$S.stats.last_rtf
        $foot = '{0} lines  ·  last: {1} ms to first sound  ·  {2:N1}× real time' -f $S.stats.requests, $S.stats.last_first_ms, $speed
    }
    if ($S.status -eq 'unreachable') { $foot = 'WSL''s SSH server is down; restarting it' }
    $g.DrawString($foot, $F.small, (New-Object System.Drawing.SolidBrush $C.faint), 14, 92)
}

function Rounded-Region([int]$w, [int]$h, [int]$r) {
    $path = New-Object System.Drawing.Drawing2D.GraphicsPath
    $path.AddArc(0, 0, $r, $r, 180, 90); $path.AddArc($w - $r, 0, $r, $r, 270, 90)
    $path.AddArc($w - $r, $h - $r, $r, $r, 0, 90); $path.AddArc(0, $h - $r, $r, $r, 90, 90); $path.CloseFigure()
    return New-Object System.Drawing.Region $path
}

if ($Snapshot) {   # sample for 6 s, then draw the card into a PNG (for checking the look over SSH)
    for ($i = 0; $i -lt 6; $i++) { Update-State; Start-Sleep -Milliseconds 1000 }
    $bmp = New-Object System.Drawing.Bitmap $CardW, $CardH
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    Draw-Card $g
    $g.Dispose()
    $bmp.Save($Snapshot, [System.Drawing.Imaging.ImageFormat]::Png)
    if ($S.keeper -and -not $S.keeper.HasExited) { $S.keeper.Kill() }
    Write-Output "status=$($S.status) gpu=$($S.gpu.util)% stats=$([bool]$S.stats)"
    exit
}

# One copy at a time.
$created = $false
$mutex = New-Object System.Threading.Mutex($true, 'FridayVoiceTray', [ref]$created)
if (-not $created) { exit }

# --- tray icon ------------------------------------------------------------------------------------
function New-DotIcon([System.Drawing.Color]$col, [int]$d) {
    $bmp = New-Object System.Drawing.Bitmap 16, 16
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = 'AntiAlias'
    $g.FillEllipse((New-Object System.Drawing.SolidBrush $col), (16 - $d) / 2, (16 - $d) / 2, $d, $d)
    $g.Dispose()
    return [System.Drawing.Icon]::FromHandle($bmp.GetHicon())
}
$Icons = @{
    off = New-DotIcon $C.off 10; ready = New-DotIcon $C.ready 12; unreachable = New-DotIcon $C.bad 12
    speaking = New-DotIcon $C.speak 15; speaking2 = New-DotIcon $C.speak 10
}

$tray = New-Object System.Windows.Forms.NotifyIcon
$tray.Icon = $Icons.off
$tray.Text = 'Friday voice: starting'
$tray.Visible = $true

# --- the overlay card -----------------------------------------------------------------------------
$form = New-Object System.Windows.Forms.Form
$form.FormBorderStyle = 'None'; $form.ShowInTaskbar = $false; $form.TopMost = $true
$form.StartPosition = 'Manual'; $form.Size = New-Object System.Drawing.Size($CardW, $CardH)
$form.BackColor = $C.bg; $form.Opacity = 0.94
$form.Region = Rounded-Region $CardW $CardH 16
$form.GetType().GetProperty('DoubleBuffered', [System.Reflection.BindingFlags]'NonPublic,Instance').SetValue($form, $true, $null)
function Place-Card {
    $wa = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
    $form.Location = New-Object System.Drawing.Point(($wa.Right - $CardW - 14), ($wa.Bottom - $CardH - 14))
}
Place-Card
$form.Add_Paint({ param($sender, $e) Draw-Card $e.Graphics })
$form.Add_Click({ $Prefs.overlay = 'speaking'; Save-Prefs })   # click: hide until she speaks again

# --- tray menu ------------------------------------------------------------------------------------
$menu = New-Object System.Windows.Forms.ContextMenuStrip
$head = $menu.Items.Add('Friday voice'); $head.Enabled = $false
[void]$menu.Items.Add('-')
$ovl = New-Object System.Windows.Forms.ToolStripMenuItem 'Show the card'
foreach ($pair in @(@('always', 'Always'), @('speaking', 'Only while she speaks'), @('never', 'Never'))) {
    $it = New-Object System.Windows.Forms.ToolStripMenuItem $pair[1]
    $it.Tag = $pair[0]
    $it.Add_Click({ param($s, $e) $Prefs.overlay = $s.Tag; Save-Prefs })
    [void]$ovl.DropDownItems.Add($it)
}
[void]$menu.Items.Add($ovl)
$startItem = $menu.Items.Add('Start the voice server')
$startItem.Add_Click({ Start-Process -WindowStyle Hidden wsl.exe -ArgumentList @('-d', $Distro, '-e', 'bash', '-lc', $ServerCmd) })
$stopItem = $menu.Items.Add('Stop the voice server')
$stopItem.Add_Click({ Start-Process -WindowStyle Hidden wsl.exe -ArgumentList @('-d', $Distro, '-e', 'pkill', '-f', '[f]riday_tts_server.py') })
$logItem = $menu.Items.Add('Open the server log')
$logItem.Add_Click({ Start-Process notepad.exe "\\wsl$\$Distro\home\aman\friday-tts\logs\server.log" })
[void]$menu.Items.Add('-')
$quitItem = $menu.Items.Add('Quit')
$quitItem.Add_Click({
    $timer.Stop(); $tray.Visible = $false
    if ($S.keeper -and -not $S.keeper.HasExited) { try { $S.keeper.Kill() } catch {} }
    [System.Windows.Forms.Application]::Exit()
})
$menu.Add_Opening({
    $head.Text = 'Friday voice: ' + (Status-Text)
    foreach ($it in $ovl.DropDownItems) { $it.Checked = ($it.Tag -eq $Prefs.overlay) }
})
$tray.ContextMenuStrip = $menu
$tray.Add_MouseClick({ param($s, $e) if ($e.Button -eq 'Left') { $Prefs.overlay = 'always'; Save-Prefs } })

# --- the loop -------------------------------------------------------------------------------------
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 1000
$timer.Add_Tick({
    Update-State
    $icon = $S.status
    if ($icon -eq 'speaking' -and -not $S.pulse) { $icon = 'speaking2' }
    $tray.Icon = $Icons[$icon]
    $tip = 'Friday voice: ' + (Status-Text)
    if ($S.gpu) { $tip += (' · GPU {0}% · {1:N1} GB' -f [int]$S.gpu.util, ($S.gpu.used / 1024)) }
    $tray.Text = $tip.Substring(0, [Math]::Min(63, $tip.Length))
    $show = ($Prefs.overlay -eq 'always') -or ($Prefs.overlay -eq 'speaking' -and $S.status -eq 'speaking')
    if ($show -and -not $form.Visible) { Place-Card; $form.Show() }
    elseif (-not $show -and $form.Visible) { $form.Hide() }
    if ($form.Visible) { $form.Invalidate() }
})
$timer.Start()
Update-State

$ctx = New-Object System.Windows.Forms.ApplicationContext
[System.Windows.Forms.Application]::Run($ctx)
