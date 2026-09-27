# Start JACK on the Focusrite, then stream interface inputs 1-8 to stream-mix.
#
# JACK must own the Focusrite ASIO driver (it is single-client), so start this
# before Ableton, then set Ableton's audio device to "JackRouter".
#
# Server host/port/path come from settings.local.ps1 next to this script.
#
#   .\sender\start_stream.ps1                  # Focusrite inputs 1-8 via JACK
#   .\sender\start_stream.ps1 -Source test     # sine tones, no JACK needed
#   .\sender\start_stream.ps1 -BufferSize 2048 # if QjackCtl counts xruns
#   .\sender\start_stream.ps1 -BitDepth 16     # 16-bit PCM if upload < ~15 Mbps
#   .\sender\start_stream.ps1 -Codec opus      # ~0.6 Mbps, for a tight uplink
#
# -BufferSize is JACK's period: bigger means fewer xruns (clicks in both the
# stream AND Ableton's recording) but more latency monitoring through Ableton.
# It does not affect the stream's delay. JACK only takes it when it starts.
param(
    [ValidateSet("jack", "test")][string]$Source = "jack",
    [string]$AsioDevice = "ASIO::Focusrite USB ASIO",
    [int]$BufferSize = 1024,
    [ValidateSet("pcm", "opus")][string]$Codec = "pcm",
    [ValidateSet(16, 24)][int]$BitDepth = 24,
    [ValidateRange(32, 650)][int]$OpusKbps = 640
)

$settings = Join-Path $PSScriptRoot "settings.local.ps1"
if (-not (Test-Path $settings)) {
    Write-Error "missing $settings (set `$StreamHost, `$StreamPort, `$StreamPath)"
    exit 1
}
. $settings

if ($Source -eq "jack") {
    $jackDir = "C:\Program Files\JACK2"
    $jackWait = Join-Path $jackDir "tools\jack_wait.exe"

    # jack_wait exits 0 either way; its last line is "running" or "not running".
    function Test-Jack { ((& $jackWait -c 2>$null) | Select-Object -Last 1) -eq "running" }

    if (-not (Test-Jack)) {
        Write-Host "starting JACK on $AsioDevice (48 kHz, $BufferSize frames)"
        Start-Process (Join-Path $jackDir "jackd.exe") -WindowStyle Minimized `
            -ArgumentList "-d", "portaudio", "-d", "`"$AsioDevice`"", "-r", "48000", "-p", "$BufferSize"
        $deadline = (Get-Date).AddSeconds(15)
        while (-not (Test-Jack) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
        if (-not (Test-Jack)) {
            Write-Error "JACK did not start. Is Ableton (or another app) still using $AsioDevice?"
            exit 1
        }
    }
    Write-Host "JACK is running. In Ableton: Preferences > Audio > Driver Type ASIO, Audio Device JackRouter."
}

# The GStreamer MSVC install's gi bindings are built for CPython 3.9.
$gst = $env:GSTREAMER_1_0_ROOT_MSVC_X86_64
if (-not $gst) { $gst = "C:\Program Files\gstreamer\1.0\msvc_x86_64\" }
$env:PATH = "$(Join-Path $gst 'bin');$env:PATH"
$env:PYTHONPATH = Join-Path $gst "lib\site-packages"

$argv = @("$PSScriptRoot\tracks_sender.py", "--host", $StreamHost, "--port", $StreamPort,
          "--path", $StreamPath, "--source", $Source,
          "--codec", $Codec, "--bit-depth", $BitDepth, "--opus-kbps", $OpusKbps)
if ($Source -eq "jack") { $argv += "--jack-autoconnect" }
py -3.9 @argv
