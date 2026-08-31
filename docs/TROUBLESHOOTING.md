# Troubleshooting

Ordered by how often each cause turns out to be the real one.

**Start here.** On the Pi, leave this running in a spare terminal:

```bash
mosquitto_sub -h localhost -u hub -P '<hub password>' -t 'pcwake/#' -v
```

You will see every status, command and ack as it happens. Most of the
questions below answer themselves in that one window: if a command appears
and no ack follows, the problem is on the PC; if the command never appears,
it is on the Pi.

---

## `/wake` does nothing

In the order they are usually to blame.

**1. Fast Startup is on (Windows).** By far the most common cause. "Shut
down" with Fast Startup enabled is really a partial hibernate, and it
typically leaves the NIC unable to wake.

```powershell
powercfg /h off
```

Then shut down and try again — the setting only takes effect from the *next*
shutdown.

**2. The NIC is not armed.**

```powershell
powercfg /devicequery wake_armed          # Windows
sudo ethtool <iface> | grep Wake-on       # Linux, want: g
```

On Windows, tick *Allow this device to wake the computer* in Device Manager →
adapter → Power Management. On Linux, `sudo ethtool -s <iface> wol g` — and
make it persistent, because it resets on reboot.

**3. Wake-on-LAN is off in the BIOS.** Look for *Wake on LAN*, *Power On By
PCI-E*, or *Resume by PCI-E Device*, usually under Power Management.

**4. The PC is on Wi-Fi.** WoWLAN is unreliable on most hardware. Use
Ethernet.

**5. The packet is not reaching the PC.** The default broadcast address does
not cross a router, so a PC on a different subnet from the Pi will never see
it. Set that subnet's directed broadcast address in the host's `broadcast`
setting (e.g. `192.168.1.255`) and confirm your router forwards directed
broadcasts — many do not, in which case the Pi must be on the same segment.

Confirm the packet leaves the Pi:

```bash
sudo tcpdump -i any -n 'udp port 9'
```

**6. The MAC is wrong.** It must be the *wired* adapter's, and it does not
change with DHCP — unlike the IP.

**7. The switch dropped the entry.** Some managed switches age out the MAC of
a machine that has been off for hours and stop forwarding to that port. Wake
it by hand once and see whether the problem only appears after long
power-offs.

---

## `/status` says 🟡 Powered on, agent not running

Exactly what it says: the PC answers pings, the agent is not connected. This
is a service problem, not a wake problem — do not go back to the BIOS.

```powershell
Get-ScheduledTask -TaskName pcwake-agent        # Windows
```
```bash
systemctl status pcwake-agent                   # Linux
journalctl -u pcwake-agent -n 50
```

On Windows this is *expected* between boot and logon: the agent starts at
logon, because locking the screen needs an interactive session.

If the agent is running but never connects, it is almost always credentials —
see below.

---

## `/status` never says 🟡 Powered on, agent not running

That state needs the reachability probe, and the probe needs two things: an
`ip` set for the host in the hub config, and a working `ping`.

The hub pings itself once at startup and logs a warning if it cannot:

```bash
journalctl -u pcwake-hub | grep -i "reachability probe"
```

If it warns, the usual cause is sandboxing. `ping` carries a file capability
(`cap_net_raw=ep`), and file capabilities are disabled under
`NoNewPrivileges=true` — so the service needs `AmbientCapabilities=CAP_NET_RAW`,
which the shipped unit sets. If you wrote your own unit, add it.

Everything else still works without the probe: online and offline are
reported from MQTT presence alone. You only lose the ability to tell a
crashed agent apart from a powered-off PC.

---

## `/status` says ⚪ Unknown

No retained status on the broker and no `ip` configured to probe, so there is
nothing to go on. Normal before the agent has ever connected. If it persists,
check the agent's log and add `ip` to the host's config so the hub can at
least tell whether the machine is up.

---

## Mosquitto warns about the password or ACL file

```
Warning: File /etc/mosquitto/aclfile has world readable permissions.
         Future versions will refuse to load this file.
Warning: File /etc/mosquitto/pcwake.passwd owner is not root.
```

Both files must be owned by root with mode 0700:

```bash
sudo chown root:root /etc/mosquitto/pcwake.passwd /etc/mosquitto/aclfile
sudo chmod 0700 /etc/mosquitto/pcwake.passwd /etc/mosquitto/aclfile
sudo systemctl restart mosquitto
```

Mosquitto reads them while it is still root and only then drops privileges,
so it does not need to own them. This is a warning today and a broker that
will not start on a future release.

---

## The agent never connects

**Credentials.** The most common cause, and `doctor` proves it in one step:

```bash
python -m pcwake.agent doctor
```

It distinguishes "cannot reach the broker" from "the broker refused these
credentials", which a plain port check cannot.

**The ACL.** `/etc/mosquitto/aclfile` ships with a host named `desk`. If your
`[agent].host` is anything else, the agent connects and is then silently
denied its own topics — which looks identical to a broken agent. The names in
the ACL, `[agent].host`, and the hub's `[[hosts]].name` must all match.

```bash
sudo tail -f /var/log/syslog | grep mosquitto
```

**The broker is not listening where you think.** `listener 1883` with no
address listens on every interface; with an address, only that one.

```bash
ss -tlnp | grep 1883
```

---

## Commands time out

*"Sent sleep but heard nothing back in 10s."*

The command reached the broker and no ack came back. In the
`mosquitto_sub` window:

- **The command does not appear** → the hub cannot publish. Check
  `journalctl -u pcwake-hub`.
- **The command appears, no ack** → the agent is not receiving or not
  answering. Check its log, and check the ACL grants it `read` on
  `pcwake/<host>/cmd`.
- **The ack appears but the bot still says it timed out** → the ack took
  longer than `ACK_TIMEOUT`. Rare on a LAN; usually a sign the PC was already
  going down.

Note that a sleep or shutdown *acknowledged* means the agent accepted it and
was about to act — the ack has to be sent before the machine goes down, so it
cannot report the outcome. The status going ⚫ Offline is the confirmation.

---

## The PC sleeps when I asked it to sleep, but hibernates

Windows `SetSuspendState` hibernates rather than sleeping whenever
hibernation is available.

```powershell
powercfg /h off
```

The same command that fixes Fast Startup. `doctor` warns about this.

---

## `/lock` fails

**Windows:** the agent is not in an interactive session. It must run as a
per-user scheduled task at logon, which is what `install-agent.ps1` sets up —
not as a SYSTEM service.

**Linux:** a system service has no session to lock. Run the agent as a *user*
service (`systemctl --user`) for `/lock` to work on a desktop. Headless
machines have nothing to lock.

---

## `/sleep` or `/shutdown` fails on Linux

*"Interactive authentication required."* The agent's user has no active
session, so logind will not authorise the action.

```bash
sudo cp install/linux/99-pcwake-power.rules /etc/polkit-1/rules.d/
sudo systemctl restart polkit
```

---

## The bot ignores me

Your chat id is not in `allowed_chat_ids`. The hub logs every rejection with
the id that sent it:

```bash
journalctl -u pcwake-hub -f
```

Message the bot, read the id out of the log, add it, restart. The bot stays
silent to unknown chats on purpose — it does not confirm it exists.

---

## The status is right but stale

Offline detection depends on the MQTT keepalive: the broker declares a client
dead at about 1.5× the keepalive, so an abruptly powered-off PC takes roughly
45 seconds to show as offline. A clean `/sleep` or `/shutdown` is faster,
because the agent publishes its own offline status on the way out.

---

## Everything worked, then stopped after a reboot

**Linux Wake-on-LAN resets on reboot.** `ethtool -s <iface> wol g` does not
persist. Use NetworkManager's `802-3-ethernet.wake-on-lan magic` or install
`pcwake-wol.service`.

**The PC's IP moved.** Waking still works (it targets the MAC), but the
reachability probe now points at the wrong address, so 🟡 *agent not running*
will never be reported. Give the PC a static DHCP lease.

**The Pi's IP moved.** The agent cannot find the broker. Give the Pi a static
lease too.

---

## Still stuck

Turn on debug logging on both sides:

```bash
pcwake-hub -v
pcwake-agent -v
```

Then reproduce with the `mosquitto_sub` window open. Between the two logs and
the topic stream, every message in the system is visible.
