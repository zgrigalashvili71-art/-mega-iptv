$ErrorActionPreference = "Stop"
$Host.UI.RawUI.WindowTitle = "MEGA IPTV Builder + Local Server"

$base = Join-Path $env:USERPROFILE "Desktop\MEGA_IPTV"
New-Item -ItemType Directory -Force -Path $base | Out-Null

$outFile = Join-Path $base "ALL_TV_PLUS_SPORTS.m3u"

$sources = @(
    @{ Name = "IPTV-ORG WORLD"; Url = "https://iptv-org.github.io/iptv/index.m3u" },
    @{ Name = "FREE-TV"; Url = "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlist.m3u8" },
    @{ Name = "PUBLIC-IPTV"; Url = "https://raw.githubusercontent.com/freecasthub/public-iptv/main/playlist.m3u" },
    @{ Name = "GEORGIA EXTRA"; Url = "https://raw.githubusercontent.com/AnikuHere/geo-iptv/main/georgia_channels.m3u" },
    @{ Name = "FAST TV EXTRA"; Url = "https://github.com/RW1986/IPTV/raw/main/lineup.m3u8" }
)

Write-Host ""
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "  BUILDING ALL TV + SPORTS MEGA PLAYLIST" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host ""

# Exact stream-URL deduplication. We intentionally do not dedupe only by channel
# name because alternate legal feeds may work when another feed is unavailable.
$seen = New-Object 'System.Collections.Generic.HashSet[string]'
$output = New-Object 'System.Collections.Generic.List[string]'
$output.Add("#EXTM3U")

$totalAdded = 0

foreach ($src in $sources) {
    Write-Host "Downloading $($src.Name)..." -ForegroundColor Yellow
    try {
        $tmp = Join-Path $base (($src.Name -replace '[^A-Za-z0-9_-]', '_') + ".m3u")
        Invoke-WebRequest -UseBasicParsing -Uri $src.Url -OutFile $tmp -TimeoutSec 45

        $lines = Get-Content -LiteralPath $tmp -Encoding UTF8
        $pending = New-Object 'System.Collections.Generic.List[string]'
        $inEntry = $false
        $added = 0

        foreach ($raw in $lines) {
            $line = $raw.Trim()
            if ([string]::IsNullOrWhiteSpace($line)) { continue }

            if ($line.StartsWith("#EXTINF", [System.StringComparison]::OrdinalIgnoreCase)) {
                $pending.Clear()
                $pending.Add($line)
                $inEntry = $true
                continue
            }

            if ($inEntry -and $line.StartsWith("#")) {
                # Preserve useful per-channel directives such as #EXTVLCOPT / #KODIPROP / #EXTGRP.
                $pending.Add($line)
                continue
            }

            if ($inEntry -and -not $line.StartsWith("#")) {
                $url = $line
                if ($url -match '^(https?|rtmp|rtsp)://') {
                    if ($seen.Add($url)) {
                        foreach ($meta in $pending) { $output.Add($meta) }
                        $output.Add($url)
                        $added++
                        $totalAdded++
                    }
                }
                $pending.Clear()
                $inEntry = $false
            }
        }

        Write-Host ("  Added {0} unique streams" -f $added) -ForegroundColor Green
    }
    catch {
        Write-Host ("  Could not load {0}: {1}" -f $src.Name, $_.Exception.Message) -ForegroundColor Red
    }
}

# Write UTF-8 without BOM, CRLF line endings for broad IPTV-player compatibility.
$text = ($output -join "`r`n") + "`r`n"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($outFile, $text, $utf8NoBom)

Write-Host ""
Write-Host "DONE." -ForegroundColor Green
Write-Host ("Unique streams in merged list: {0}" -f $totalAdded) -ForegroundColor Green
Write-Host ("Playlist file: {0}" -f $outFile) -ForegroundColor White
Write-Host ""

# Find a LAN IPv4 address, preferring 192.168.x.x / 10.x.x.x / 172.16-31.x.x.
$ipCandidates = @()
try {
    $ipCandidates = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
        Where-Object {
            $_.IPAddress -notlike "127.*" -and
            $_.IPAddress -notlike "169.254*" -and
            $_.InterfaceAlias -notmatch "Loopback|vEthernet|Virtual|VPN"
        } |
        Sort-Object InterfaceMetric
} catch {}

$lanIp = $null
foreach ($item in $ipCandidates) {
    if ($item.IPAddress -match '^192\.168\.' -or
        $item.IPAddress -match '^10\.' -or
        $item.IPAddress -match '^172\.(1[6-9]|2[0-9]|3[01])\.') {
        $lanIp = $item.IPAddress
        break
    }
}
if (-not $lanIp -and $ipCandidates.Count -gt 0) {
    $lanIp = $ipCandidates[0].IPAddress
}
if (-not $lanIp) {
    $lanIp = "YOUR-LAPTOP-IP"
}

$port = 8080

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "  AQUAPLAY URL" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host ("http://{0}:{1}/ALL_TV_PLUS_SPORTS.m3u" -f $lanIp, $port) -ForegroundColor Magenta
Write-Host ""
Write-Host "Add the URL above as ONE M3U playlist in AquaPlay." -ForegroundColor White
Write-Host "Keep this window open while watching." -ForegroundColor Yellow
Write-Host "TV box and laptop must be on the same home network." -ForegroundColor Yellow
Write-Host ""
Write-Host "Starting local playlist server on port $port..." -ForegroundColor Cyan
Write-Host "Press Ctrl+C to stop it." -ForegroundColor DarkGray
Write-Host ""

# Tiny HTTP server using TcpListener; no Python required.
$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Any, $port)

try {
    $listener.Start()
} catch {
    Write-Host "Could not start server. Windows Firewall may be blocking port $port." -ForegroundColor Red
    Write-Host "Try running this script as Administrator, then allow Private networks if Windows asks." -ForegroundColor Yellow
    Read-Host "Press Enter to close"
    exit 1
}

try {
    while ($true) {
        $client = $listener.AcceptTcpClient()
        try {
            $stream = $client.GetStream()
            $reader = New-Object System.IO.StreamReader($stream, [System.Text.Encoding]::ASCII, $false, 1024, $true)

            # Read request headers.
            while ($true) {
                $requestLine = $reader.ReadLine()
                if ($null -eq $requestLine -or $requestLine -eq "") { break }
            }

            $body = [System.IO.File]::ReadAllBytes($outFile)
            $header = "HTTP/1.1 200 OK`r`n" +
                      "Content-Type: audio/x-mpegurl; charset=utf-8`r`n" +
                      "Content-Length: $($body.Length)`r`n" +
                      "Cache-Control: no-cache`r`n" +
                      "Access-Control-Allow-Origin: *`r`n" +
                      "Connection: close`r`n`r`n"

            $headerBytes = [System.Text.Encoding]::ASCII.GetBytes($header)
            $stream.Write($headerBytes, 0, $headerBytes.Length)
            $stream.Write($body, 0, $body.Length)
            $stream.Flush()
        }
        catch {
            # Ignore individual client disconnects.
        }
        finally {
            $client.Close()
        }
    }
}
finally {
    $listener.Stop()
}
