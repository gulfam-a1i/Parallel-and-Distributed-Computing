# Network setup

The client and the worker talk over a plain TCP connection on port **5050**.
They need to be on the same subnet, and the worker's firewall has to let that
port in. Use one of the two layouts below.

| Role | IP address | Subnet mask | Gateway |
|---|---|---|---|
| Worker (GPU machine) | `192.168.1.1` | `255.255.255.0` | leave empty |
| Client (laptop) | `192.168.1.2` | `255.255.255.0` | leave empty |

Leaving the gateway empty on a direct link is intentional. It stops Windows
from trying to route internet traffic through the cable, so Wi-Fi keeps
working for internet while the cable carries the render traffic.

---

## Option A: direct Ethernet cable (recommended)

Any Cat5e/Cat6 cable works. Modern network cards do auto MDI-X, so a normal
cable is fine and you don't need a crossover cable. A gigabit link gives about
110 MB/s of real throughput, which keeps upload time small compared to render
time.

### Windows (both machines)

GUI:

1. Settings → Network & Internet → Ethernet → (your adapter) → **IP assignment → Edit**
2. Switch to **Manual**, enable **IPv4**
3. IP address `192.168.1.1` (worker) or `192.168.1.2` (client), subnet mask `255.255.255.0`
4. Leave gateway and DNS empty → Save

Command line (Administrator PowerShell). First check the adapter name with
`Get-NetAdapter`:

```powershell
# on the worker
New-NetIPAddress -InterfaceAlias "Ethernet" -IPAddress 192.168.1.1 -PrefixLength 24
# on the client
New-NetIPAddress -InterfaceAlias "Ethernet" -IPAddress 192.168.1.2 -PrefixLength 24
```

To undo it later: `Remove-NetIPAddress -InterfaceAlias "Ethernet" -IPAddress 192.168.1.x`
and set the adapter back to DHCP.

### Linux

NetworkManager (Ubuntu, Fedora, ...):

```bash
nmcli con add type ethernet ifname eth0 con-name render-link \
      ipv4.method manual ipv4.addresses 192.168.1.1/24 ipv6.method disabled
nmcli con up render-link
```

Use `192.168.1.2/24` on the client, and replace `eth0` with your interface
name from `ip link`.

Temporary, without NetworkManager (lost on reboot):

```bash
sudo ip addr add 192.168.1.1/24 dev eth0
sudo ip link set eth0 up
```

---

## Option B: dedicated Wi-Fi subnet

Use this if there is no cable. Expect lower and less stable throughput, so
upload time will be a much bigger part of the total. The benchmark shows this
clearly.

**B1: worker runs a hotspot.** On Windows go to Settings → Network & Internet →
Mobile hotspot, share over Wi-Fi, and connect the laptop to it. Windows gives
the hotspot the address `192.168.137.1`, so use that as the server IP in the
client.

**B2: both on a router you control.** Reserve fixed addresses for both
machines in the router's DHCP settings, using the router's own range (for
example `192.168.0.10` for the worker and `192.168.0.11` for the client).
You can also set static addresses as in Option A, with the router as the
gateway. Avoid university/public Wi-Fi: these networks
usually isolate clients from each other, so the two machines can't see each
other at all.

---

## Open the firewall on the worker

### Windows

Administrator PowerShell:

```powershell
New-NetFirewallRule -DisplayName "Remote Render Worker" -Direction Inbound `
    -Protocol TCP -LocalPort 5050 -Action Allow -Profile Any
# allow ping, so the reachability test works
New-NetFirewallRule -DisplayName "ICMPv4 echo" -Protocol ICMPv4 -IcmpType 8 `
    -Direction Inbound -Action Allow -Profile Any
```

A new direct cable link usually comes up as a **Public** network on Windows,
and Public blocks almost everything. `-Profile Any` covers that. You can also
switch the link to Private under Ethernet → Network profile type.

### Linux

```bash
sudo ufw allow 5050/tcp          # Ubuntu
# or
sudo firewall-cmd --add-port=5050/tcp --permanent && sudo firewall-cmd --reload
```

---

## Check the link before starting anything

From the client:

```bash
ping 192.168.1.1
```

Then, with the worker running, test the actual service:

```bash
python -m client.cli --server 192.168.1.1 --token <token> ping
```

```
connected to 'GPU-DESKTOP'  GPU: NVIDIA GeForce RTX 3060  encoders: h264_nvenc, hevc_nvenc
latency  min 0.31 / avg 0.42 / max 0.58 ms  jitter 0.09 ms  lost 0/5
```

(The values above only show what the output looks like. Yours will depend on
your hardware.)

### If it doesn't connect

| Symptom | Likely cause |
|---|---|
| `ping` times out | wrong IP/mask, cable not seated, or ICMP blocked by the firewall |
| ping works, client says **refused** | worker not running, or running on another port |
| ping works, client **times out** | firewall rule missing for TCP 5050 |
| **worker rejected the access token** | `--token` on the worker and the token in the client don't match |
| connects but mode shows **CPU fallback** | NVIDIA driver too old or FFmpeg build has no NVENC, see the README |
