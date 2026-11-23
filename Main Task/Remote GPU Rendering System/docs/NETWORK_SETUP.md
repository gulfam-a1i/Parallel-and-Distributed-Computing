# Network setup

The client and the worker talk over plain TCP on port **5050**. The program
doesn't care whether the link is a cable or Wi-Fi. The only requirements are
that both machines are on the same subnet and the worker's firewall lets
port 5050 in.

Pick the layout that matches your hardware:

| Option | What you need | Worker IP | Client IP | Notes |
|---|---|---|---|---|
| **A1. Windows Mobile Hotspot** | two laptops, no router | `192.168.137.1` (fixed by Windows) | `192.168.137.2` | dedicated Wi-Fi subnet, recommended when there is no cable |
| **A2. Phone hotspot** | two laptops + a phone | assigned by phone, then set static | assigned by phone, then set static | easiest; the phone acts as the router |
| **A3. Home Wi-Fi router** | router you control | e.g. `192.168.0.10` | e.g. `192.168.0.11` | fine at home; won't work on campus/public Wi-Fi |
| **B. Direct Ethernet cable** | one Cat5e/Cat6 cable | `192.168.1.1` | `192.168.1.2` | fastest and most stable |

The **worker** is whichever machine has the NVIDIA GPU. The other one is the
client.

After setting up any option, do the [firewall step](#open-the-firewall-on-the-worker)
and the [link check](#check-the-link).

---

## Option A1: Windows Mobile Hotspot (two laptops, no cable, no router)

The worker laptop creates its own Wi-Fi network and the client joins it. This
gives a dedicated subnet that nothing else is on, which is what the
assignment means by a "dedicated Wi-Fi subnet".

**On the worker laptop**

1. Settings → Network & Internet → **Mobile hotspot**
2. *Share my internet connection from*: Wi-Fi (or Ethernet, whatever is connected)
3. *Share over*: **Wi-Fi**
4. Click **Edit** to set a network name and password, e.g. `PDC-Render` / `render1234`
5. Turn **Mobile hotspot** on

Windows always gives the hotspot host the address **192.168.137.1**, so that
is the worker IP. Check with `ipconfig`: look for the adapter called
*Local Area Connection\* N* (Microsoft Wi-Fi Direct Virtual Adapter).

> Windows only allows the hotspot while the laptop is connected to *some*
> network it can share. Connect the worker to any Wi-Fi or a phone's hotspot
> first. If the toggle stays greyed out, use Option A2 instead.

**On the client laptop**

1. Connect to the `PDC-Render` Wi-Fi network
2. It gets an address like `192.168.137.xxx` automatically, and that already works
3. Optional, to have a fixed address: Settings → Network & Internet → Wi-Fi →
   `PDC-Render` properties → IP assignment → **Edit** → Manual → IPv4 on →
   IP `192.168.137.2`, mask `255.255.255.0`, gateway `192.168.137.1`, DNS `8.8.8.8`

In the client app, use server IP **192.168.137.1**.

---

## Option A2: phone hotspot (both laptops join the phone)

1. Turn on the hotspot on a phone
2. Connect **both** laptops to it
3. On each laptop run `ipconfig` and note the *IPv4 Address* of the Wi-Fi
   adapter, e.g. worker `192.168.43.120`, client `192.168.43.57`
   (Android usually uses `192.168.43.x` and iPhone `172.20.10.x`)

That already works: put the worker's address in the client app. To keep
the addresses from changing between sessions, set them as static on each
laptop (Wi-Fi properties → IP assignment → Manual). Use the same first three
numbers the phone gave you, the phone's address as gateway (`192.168.43.1` or
`172.20.10.1`) and mask `255.255.255.0` (iPhone: `255.255.255.240`). For example:

| Laptop | IP | Mask | Gateway |
|---|---|---|---|
| Worker | `192.168.43.10` | `255.255.255.0` | `192.168.43.1` |
| Client | `192.168.43.11` | `255.255.255.0` | `192.168.43.1` |

Some phones have a "client isolation" or "AP isolation" setting that stops
connected devices from seeing each other. If ping between the laptops fails,
turn that off or use Option A1.

---

## Option A3: home Wi-Fi router

Connect both laptops to the same router. Either reserve fixed addresses for
both in the router's DHCP settings, or set static addresses on each laptop as
in A2, using the router's range and the router as gateway.

Campus, office and café Wi-Fi almost always isolate clients from each other,
so the laptops can't connect. Use A1 or A2 there.

---

## Option B: direct Ethernet cable

Any Cat5e/Cat6 cable works. Modern network cards swap the wiring
automatically (auto MDI-X), so you don't need a crossover cable. A gigabit
link gives about 110 MB/s, so uploads are much faster than over Wi-Fi.

| Role | IP address | Subnet mask | Gateway |
|---|---|---|---|
| Worker | `192.168.1.1` | `255.255.255.0` | leave empty |
| Client | `192.168.1.2` | `255.255.255.0` | leave empty |

Leave the gateway empty on purpose. Windows then won't try to send internet
traffic down the cable, so Wi-Fi keeps working for internet at the same time.

**Windows, GUI:** Settings → Network & Internet → Ethernet → IP assignment →
**Edit** → Manual → IPv4 on → enter the IP and mask above → Save.

**Windows, command line** (Administrator PowerShell; find the adapter name with `Get-NetAdapter`):

```powershell
New-NetIPAddress -InterfaceAlias "Ethernet" -IPAddress 192.168.1.1 -PrefixLength 24   # worker
New-NetIPAddress -InterfaceAlias "Ethernet" -IPAddress 192.168.1.2 -PrefixLength 24   # client
```

**Linux:**

```bash
nmcli con add type ethernet ifname eth0 con-name render-link \
      ipv4.method manual ipv4.addresses 192.168.1.1/24 ipv6.method disabled
nmcli con up render-link
```

(Use `192.168.1.2/24` on the client, and your interface name from `ip link`.)

---

## Open the firewall on the worker

Needed for every option. Run in an **Administrator PowerShell** on the worker:

```powershell
New-NetFirewallRule -DisplayName "Remote Render Worker" -Direction Inbound `
    -Protocol TCP -LocalPort 5050 -Action Allow -Profile Any
# allow ping, so the reachability test works
New-NetFirewallRule -DisplayName "ICMPv4 echo" -Protocol ICMPv4 -IcmpType 8 `
    -Direction Inbound -Action Allow -Profile Any
```

New hotspot and cable links usually come up as **Public** networks, and
Windows blocks almost everything on Public. `-Profile Any` covers that.

Linux: `sudo ufw allow 5050/tcp`

---

## Check the link

From the client (replace the IP with your worker's):

```bash
ping 192.168.137.1
```

Then start the worker and test the actual service:

```bash
python -m client.cli --server 192.168.137.1 --token <token> ping
```

Or press **Test connection** in the GUI. The status panel shows the measured
latency.

Expect very different numbers depending on the link: a cable is usually
under 1 ms with about 900 Mbit/s of throughput, while Wi-Fi is typically
2–10 ms and a fraction of that throughput. The benchmark measures exactly
this. When you run it, pass the link type so the report records it:

```bash
python -m benchmark.run_benchmark --server 192.168.137.1 --token <token> --network "Wi-Fi (Windows hotspot)"
```

### If it doesn't connect

| Symptom | Likely cause |
|---|---|
| `ping` times out | not on the same network, wrong IP, phone/router client isolation, or ICMP blocked by the firewall |
| ping works, client says **refused** | worker not running, or running on another port |
| ping works, client **times out** | firewall rule for TCP 5050 missing |
| **worker rejected the access token** | `--token` on the worker and the token in the client don't match |
| worked yesterday, not today | phone/router gave a new IP; run `ipconfig` again or set static IPs |
| hotspot toggle greyed out | the worker laptop isn't connected to any network it can share; use the phone hotspot instead |
