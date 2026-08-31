# Setup

About an hour end to end, most of it waiting for a Pi to install packages.

Order matters: the BIOS and NIC settings come first, because everything else
is easy to verify and those are the ones that silently do nothing.

- [1. The PC's firmware and NIC](#1-the-pcs-firmware-and-nic)
- [2. The Pi: broker](#2-the-pi-broker)
- [3. The Pi: Telegram bot](#3-the-pi-telegram-bot)
- [4. The Pi: hub service](#4-the-pi-hub-service)
- [5. The PC: agent](#5-the-pc-agent)
- [6. End-to-end check](#6-end-to-end-check)

You will need: the PC's **wired MAC address**, a **static DHCP lease** for
both the PC and the Pi, and a Telegram account.

---

## 1. The PC's firmware and NIC

**Use Ethernet.** Wake-on-Wireless-LAN exists, and it is unreliable on most
hardware. If the PC is on Wi-Fi, expect waking not to work.

### BIOS/UEFI

Enable the setting named *Wake on LAN*, *Power On By PCI-E/PCI*, *Resume by
PCI-E Device*, or similar — vendors disagree about the wording. It is usually
under Power Management.

### Windows

Three things, and all three matter:

**Disable Fast Startup and hibernation.** This is the single most common
reason waking fails. With Fast Startup on, "shut down" is really a partial
hibernate that typically leaves the NIC unable to wake. It also makes
`/sleep` hibernate instead of sleeping. From an elevated prompt:

```powershell
powercfg /h off
```

**Arm the network adapter.** Device Manager → your network adapter →
Properties → Power Management → tick both:

- *Allow this device to wake the computer*
- *Only allow a magic packet to wake the computer*

Then, on the Advanced tab, if *Wake on Magic Packet* is listed, enable it.

**Verify:**

```powershell
powercfg /devicequery wake_armed
```

Your adapter should be listed. `python -m pcwake.agent doctor` checks all
three once the agent is installed.

### Linux

```bash
ip route get 1.1.1.1              # names the interface, e.g. enp3s0
sudo ethtool enp3s0 | grep Wake-on
```

`Wake-on: g` means magic packets will wake it. If it says `d`:

```bash
sudo ethtool -s enp3s0 wol g
```

**This does not survive a reboot.** Make it persistent — with NetworkManager:

```bash
nmcli connection modify "<connection name>" 802-3-ethernet.wake-on-lan magic
```

Otherwise install `install/linux/pcwake-wol.service`, editing the interface
name.

---

## 2. The Pi: broker

```bash
sudo apt update
sudo apt install -y mosquitto mosquitto-clients python3-pip python3-venv
```

Create the two accounts — one for the hub, one for the agent — so a
compromised agent cannot issue commands:

```bash
sudo mosquitto_passwd -c /etc/mosquitto/pcwake.passwd hub
sudo mosquitto_passwd    /etc/mosquitto/pcwake.passwd agent
```

Install the config and the topic ACL:

```bash
sudo cp install/pi/mosquitto/pcwake.conf /etc/mosquitto/conf.d/
sudo cp install/pi/mosquitto/aclfile /etc/mosquitto/
sudo chown mosquitto: /etc/mosquitto/pcwake.passwd /etc/mosquitto/aclfile
sudo chmod 600 /etc/mosquitto/pcwake.passwd
sudo systemctl restart mosquitto
```

The ACL ships with one host named `desk`. If your PC has a different name,
edit `/etc/mosquitto/aclfile` to match before going further — a mismatch here
looks exactly like a broken agent.

Check it:

```bash
mosquitto_sub -h localhost -u hub -P '<hub password>' -t 'pcwake/#' -v
```

That subscription is the single most useful debugging tool in this project.
Leave it running in a spare terminal for the rest of the setup.

---

## 3. The Pi: Telegram bot

Message [@BotFather](https://t.me/botfather) → `/newbot` → follow the
prompts. Keep the token it gives you; it is the only credential.

Optionally give the bot a command menu with `/setcommands`:

```
status - show the PC's state
wake - send a Wake-on-LAN magic packet
sleep - suspend to RAM
lock - lock the screen
shutdown - power off
restart - reboot
remote - remote desktop links
```

**Find your chat id.** Send any message to your new bot, then start the hub
once by hand (step 4) and read the log — an unauthorized message logs the id
that sent it. That number goes in `allowed_chat_ids`.

---

## 4. The Pi: hub service

```bash
sudo useradd --system --home /opt/pcwake --shell /usr/sbin/nologin pcwake
sudo mkdir -p /opt/pcwake /etc/pcwake
sudo python3 -m venv /opt/pcwake/venv
sudo /opt/pcwake/venv/bin/pip install ".[hub]"

sudo cp config.example.toml /etc/pcwake/config.toml
sudo chmod 600 /etc/pcwake/config.toml
sudo nano /etc/pcwake/config.toml
```

Fill in `[broker]` (username `hub`), `[telegram]`, and one `[[hosts]]` block.
Leave `[agent]` alone — the hub ignores it.

Keeping the secrets out of the config file, if you prefer:

```bash
sudo tee /etc/pcwake/pcwake.env >/dev/null <<'ENV'
PCWAKE_TELEGRAM_TOKEN=123456:ABC...
PCWAKE_BROKER_PASSWORD=...
ENV
sudo chmod 600 /etc/pcwake/pcwake.env
```

Run it by hand first — this is where you will read your chat id:

```bash
sudo -u pcwake /opt/pcwake/venv/bin/pcwake-hub --config /etc/pcwake/config.toml -v
```

Message the bot. The log prints the chat id of anything it rejects. Put that
number in `allowed_chat_ids`, restart, and `/help` should answer.

Then install the service:

```bash
sudo cp install/pi/pcwake-hub.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pcwake-hub
journalctl -u pcwake-hub -f
```

At this point `/wake` already works, even with no agent installed. It will
report that the PC never came online — the agent is what closes that loop.

---

## 5. The PC: agent

### Windows

```powershell
pip install ".[agent]"
mkdir $env:ProgramData\pcwake
copy config.example.toml $env:ProgramData\pcwake\config.toml
notepad $env:ProgramData\pcwake\config.toml
```

Set `[broker].host` to the Pi's address, username to `agent`, and
`[agent].host` to the name in the hub's `[[hosts]]` block. **Leave
`dry_run = true` for now.**

```powershell
python -m pcwake.agent --config $env:ProgramData\pcwake\config.toml doctor
```

Clear every `[FAIL]` before continuing. Then install the scheduled task:

```powershell
.\install\windows\install-agent.ps1
```

This installs a per-user task that starts at logon rather than a SYSTEM
service, because `LockWorkStation` needs an interactive session. The
trade-off: the agent is not running between boot and logon, so the PC reads
as offline in that window. Waking still works — that is the NIC's job.

### Linux

```bash
sudo useradd --system --home /opt/pcwake --shell /usr/sbin/nologin pcwake
sudo mkdir -p /opt/pcwake /etc/pcwake
sudo python3 -m venv /opt/pcwake/venv
sudo /opt/pcwake/venv/bin/pip install ".[agent]"
sudo cp config.example.toml /etc/pcwake/config.toml
sudo chmod 600 /etc/pcwake/config.toml
sudo nano /etc/pcwake/config.toml

sudo /opt/pcwake/venv/bin/pcwake-agent --config /etc/pcwake/config.toml doctor
sudo cp install/linux/99-pcwake-power.rules /etc/polkit-1/rules.d/
sudo cp install/linux/pcwake-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pcwake-agent
```

The polkit rule is what lets the unprivileged `pcwake` user suspend and power
off. On a desktop with an active graphical session you may not need it; on a
headless machine you will.

For `/lock` on a desktop, run the agent as a *user* service instead — a
system service has no session to lock.

---

## 6. End-to-end check

Run these in order, from the phone only. Each one has a specific failure it
is there to catch.

```
/status     🟢 Online
```
If it says 🟡 *Powered on, agent not running*, the PC is up but the agent is
not connected — check the agent's log, not the BIOS.

```
/lock       screen locks, ✅ acknowledged
```
Proves the command path works while the machine stays up.

**Now turn off dry-run.** Set `dry_run = false`, restart the agent, confirm
`/status` no longer shows the dry-run warning.

```
/sleep      PC sleeps, /status → ⚫ Offline within ~45s
/wake       🔵 Waking… → 🟢 Online
```

Then the one that matters most, because a cold power-off is a stricter test
of Wake-on-LAN than sleep is:

```
/shutdown   confirm → PC powers off
/wake       🔵 Waking… → 🟢 Online
```

And the state that distinguishes this from a naive status check — stop the
agent while leaving the PC running:

```
/status     🟡 Powered on, agent not running
```

Finally, reboot both machines and confirm both services come back with no
login. On Windows the agent starts at logon, so the PC reads as offline
until you sign in.

If any step fails, [TROUBLESHOOTING.md](TROUBLESHOOTING.md) is ordered by how
often each cause is the real one.
