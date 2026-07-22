param(
    [Parameter(Mandatory = $true)][string]$Config,
    [Parameter(Mandatory = $true)][string]$EventKey,
    [Parameter(Mandatory = $true)][string]$MessageBase64,
    [switch]$Send,
    [switch]$VerifyDraftOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ($Send.IsPresent -eq $VerifyDraftOnly.IsPresent) {
    throw 'Choose exactly one of -Send or -VerifyDraftOnly.'
}

$message = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($MessageBase64))
$pythonHelper = Join-Path $PSScriptRoot 'wechat_push.py'
$dryOutput = & python $pythonHelper send --config $Config --event-key $EventKey --message-b64 $MessageBase64 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "Exact WeChat target verification failed: $($dryOutput -join ' ')"
}

Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
Add-Type @'
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class SoccerWechatNative {
    public delegate bool EnumProc(IntPtr hWnd, IntPtr lParam);
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc callback, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int count);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr hWnd, StringBuilder text, int count);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int command);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")] public static extern void mouse_event(uint flags, uint dx, uint dy, uint data, UIntPtr extra);
    [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr value);
}
'@

try { [void][SoccerWechatNative]::SetProcessDpiAwarenessContext([IntPtr](-4)) } catch {}

$windows = New-Object System.Collections.Generic.List[System.IntPtr]
$callback = [SoccerWechatNative+EnumProc]{
    param($handle, $parameter)
    if ([SoccerWechatNative]::IsWindowVisible($handle)) {
        $title = New-Object Text.StringBuilder 64
        $class = New-Object Text.StringBuilder 128
        [void][SoccerWechatNative]::GetWindowText($handle, $title, $title.Capacity)
        [void][SoccerWechatNative]::GetClassName($handle, $class, $class.Capacity)
        if ($title.ToString() -eq 'Weixin' -and $class.ToString() -eq 'Qt51514QWindowIcon') {
            $windows.Add($handle)
        }
    }
    return $true
}
[void][SoccerWechatNative]::EnumWindows($callback, [IntPtr]::Zero)
if ($windows.Count -ne 1) { throw "Expected exactly one visible WeChat window; found $($windows.Count)." }
$wechat = $windows[0]
$rect = New-Object SoccerWechatNative+RECT
if (-not [SoccerWechatNative]::GetWindowRect($wechat, [ref]$rect)) { throw 'Could not read WeChat bounds.' }
[void][SoccerWechatNative]::ShowWindow($wechat, 9)
[void][SoccerWechatNative]::SetForegroundWindow($wechat)
Start-Sleep -Milliseconds 350

function Click-Point([int]$x, [int]$y) {
    [void][SoccerWechatNative]::SetCursorPos($x, $y)
    [SoccerWechatNative]::mouse_event(2, 0, 0, 0, [UIntPtr]::Zero)
    [SoccerWechatNative]::mouse_event(4, 0, 0, 0, [UIntPtr]::Zero)
}

function Test-SendGreen {
    $bitmap = New-Object Drawing.Bitmap 40, 20
    $graphics = [Drawing.Graphics]::FromImage($bitmap)
    try {
        $graphics.CopyFromScreen($rect.Right - 80, $rect.Bottom - 52, 0, 0, $bitmap.Size)
        $green = 0
        $total = 0
        for ($x = 0; $x -lt $bitmap.Width; $x++) {
            for ($y = 0; $y -lt $bitmap.Height; $y++) {
                $pixel = $bitmap.GetPixel($x, $y)
                $total++
                if ($pixel.G -gt ($pixel.R + 30) -and $pixel.G -gt ($pixel.B + 20) -and $pixel.G -gt 100) { $green++ }
            }
        }
        return (($green / $total) -ge 0.50)
    }
    finally { $graphics.Dispose(); $bitmap.Dispose() }
}

function Save-Proof([string]$path) {
    $bitmap = New-Object Drawing.Bitmap ($rect.Right - $rect.Left), ($rect.Bottom - $rect.Top)
    $graphics = [Drawing.Graphics]::FromImage($bitmap)
    try { $graphics.CopyFromScreen($rect.Left, $rect.Top, 0, 0, $bitmap.Size); $bitmap.Save($path, [Drawing.Imaging.ImageFormat]::Png) }
    finally { $graphics.Dispose(); $bitmap.Dispose() }
}

if (Test-SendGreen) { throw 'Configured WeChat target already contains an unsent draft.' }

$oldClipboard = [Windows.Forms.Clipboard]::GetDataObject()
try {
    Click-Point ($rect.Left + 430) ($rect.Bottom - 126)
    Start-Sleep -Milliseconds 350
    [Windows.Forms.Clipboard]::SetText($message)
    Start-Sleep -Milliseconds 150
    [Windows.Forms.SendKeys]::SendWait('^v')
    Start-Sleep -Milliseconds 1200
}
finally {
    if ($null -ne $oldClipboard) { [Windows.Forms.Clipboard]::SetDataObject($oldClipboard, $true) }
    else { [Windows.Forms.Clipboard]::Clear() }
}

$safeKey = $EventKey -replace '[^A-Za-z0-9._-]', '-'
$configDirectory = Split-Path -Parent ([IO.Path]::GetFullPath($Config))
$draftProof = Join-Path $configDirectory "wechat_ps_draft_$safeKey.png"
Save-Proof $draftProof
if (-not (Test-SendGreen)) {
    Click-Point ($rect.Left + 430) ($rect.Bottom - 126)
    [Windows.Forms.SendKeys]::SendWait('^a')
    [Windows.Forms.SendKeys]::SendWait('{BACKSPACE}')
    throw "WeChat did not confirm the pasted draft; nothing was sent; proof=$draftProof"
}

if ($VerifyDraftOnly) {
    Click-Point ($rect.Left + 430) ($rect.Bottom - 126)
    [Windows.Forms.SendKeys]::SendWait('^a')
    [Windows.Forms.SendKeys]::SendWait('{BACKSPACE}')
    Start-Sleep -Milliseconds 500
    if (Test-SendGreen) { throw 'Draft self-test could not confirm cleanup.' }
    [pscustomobject]@{ sent = $false; draft_verified = $true; draft_cleared = $true; event_key = $EventKey } | ConvertTo-Json
    exit 0
}

$statePath = Join-Path $configDirectory 'wechat_push_state.json'
$deliveries = [ordered]@{}
if (Test-Path -LiteralPath $statePath) {
    $existingState = Get-Content -LiteralPath $statePath -Encoding UTF8 -Raw | ConvertFrom-Json
    foreach ($property in $existingState.deliveries.PSObject.Properties) { $deliveries[$property.Name] = $property.Value }
}
if ($deliveries.Contains($EventKey)) { throw "Delivery event already exists: $EventKey" }
$sha = [BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($message))).Replace('-', '').ToLowerInvariant()
$deliveries[$EventKey] = [ordered]@{ status='attempting'; chat_name='打野'; message_sha256=$sha; updated_at=[DateTimeOffset]::Now.ToString('yyyy-MM-ddTHH:mm:sszzz') }
$state = [ordered]@{ version=1; deliveries=$deliveries }
$json = $state | ConvertTo-Json -Depth 8
[IO.File]::WriteAllText($statePath + '.tmp', $json + "`n", (New-Object Text.UTF8Encoding($false)))
Move-Item -LiteralPath ($statePath + '.tmp') -Destination $statePath -Force

Click-Point ($rect.Right - 60) ($rect.Bottom - 42)
Start-Sleep -Milliseconds 1200
$proofPath = Join-Path $configDirectory "wechat_ps_sent_$safeKey.png"
Save-Proof $proofPath
if (Test-SendGreen) { throw 'Send was clicked but delivery could not be confirmed; retry is blocked.' }

$deliveries[$EventKey].status = 'sent'
$deliveries[$EventKey].updated_at = [DateTimeOffset]::Now.ToString('yyyy-MM-ddTHH:mm:sszzz')
$deliveries[$EventKey].proof = [IO.Path]::GetFileName($proofPath)
$state = [ordered]@{ version=1; deliveries=$deliveries }
$json = $state | ConvertTo-Json -Depth 8
[IO.File]::WriteAllText($statePath + '.tmp', $json + "`n", (New-Object Text.UTF8Encoding($false)))
Move-Item -LiteralPath ($statePath + '.tmp') -Destination $statePath -Force
[pscustomobject]@{ sent=$true; chat_name='打野'; event_key=$EventKey; proof=$proofPath } | ConvertTo-Json
