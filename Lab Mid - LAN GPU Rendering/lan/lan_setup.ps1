# Configures the direct Ethernet cable between the two laptops.
#
#   worker (GPU laptop)  -> 192.168.1.1/24, opens TCP 5050 in the firewall
#   client (this laptop) -> 192.168.1.2/24
#
# Must run as Administrator (the .bat launchers elevate automatically).
# Only the wired adapter is changed; Wi-Fi and internet are left alone.

param(
    [Parameter(Mandatory = $true)][ValidateSet("worker", "client")][string]$Role,
    [int]$Port = 5050
)

$ErrorActionPreference = "Stop"
$ip   = if ($Role -eq "worker") { "192.168.1.1" } else { "192.168.1.2" }
$peer = if ($Role -eq "worker") { "192.168.1.2" } else { "192.168.1.1" }

function Say($msg, $color = "Gray") { Write-Host $msg -ForegroundColor $color }

# 1. Find the wired adapter that has the cable plugged in.
$nic = Get-NetAdapter -Physical |
       Where-Object { $_.Status -eq "Up" -and $_.MediaType -eq "802.3" -and $_.InterfaceDescription -notmatch "Virtual|TAP|VPN" } |
       Select-Object -First 1
if (-not $nic) {
    Say "No connected Ethernet adapter found. Check that the LAN cable is plugged into both laptops." Red
    Get-NetAdapter | Format-Table Name, InterfaceDescription, Status, LinkSpeed -AutoSize
    exit 1
}
Say ("Ethernet adapter: {0} ({1}), link {2}" -f $nic.Name, $nic.InterfaceDescription, $nic.LinkSpeed) Cyan

# 2. Static IP (no gateway, so internet keeps going over Wi-Fi).
$current = Get-NetIPAddress -InterfaceIndex $nic.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue
if ($current.IPAddress -contains $ip) {
    Say "IP $ip already set." Green
} else {
    Set-NetIPInterface -InterfaceIndex $nic.ifIndex -Dhcp Disabled
    $current | Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue
    New-NetIPAddress -InterfaceIndex $nic.ifIndex -IPAddress $ip -PrefixLength 24 | Out-Null
    Say "IP set to $ip / 255.255.255.0" Green
}

# 3. Mark the cable network Private (it is shown as an unidentified Public network otherwise).
try {
    Start-Sleep -Seconds 2
    Set-NetConnectionProfile -InterfaceIndex $nic.ifIndex -NetworkCategory Private -ErrorAction Stop
    Say "Network profile set to Private." Green
} catch { Say "Could not change network profile (not critical): $($_.Exception.Message)" Yellow }

# 4. Firewall: allow ping on both; open the worker port on the GPU laptop.
if (-not (Get-NetFirewallRule -DisplayName "LAN Render - Ping" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName "LAN Render - Ping" -Direction Inbound -Protocol ICMPv4 -IcmpType 8 -Action Allow -Profile Any | Out-Null
}
if ($Role -eq "worker" -and -not (Get-NetFirewallRule -DisplayName "Remote Render Worker" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName "Remote Render Worker" -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow -Profile Any | Out-Null
}
Say "Firewall rules OK." Green

# 5. Check the other laptop.
Say "`nPinging the other laptop ($peer)..." Cyan
if (Test-Connection -ComputerName $peer -Count 3 -Quiet) {
    Say "Other laptop reachable at $peer over the cable." Green
} else {
    Say "No reply from $peer yet. That is normal if the other laptop has not run its setup yet." Yellow
}
exit 0
