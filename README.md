# pcwake

Wake, check on, and power-control a PC from your phone — from anywhere,
without opening a single port.

```
/status   🟢 Online
/sleep    ✅ desk: sleep acknowledged
/wake     🔵 Waking… → 🟢 Online
```

## Why there are two pieces

When the PC is off, nothing on the PC can listen. That one fact decides the
whole design: something else on the LAN has to send the Wake-on-LAN magic
packet. Here that is a Raspberry Pi, which also runs the MQTT broker and the
Telegram bot.

```
  Phone (Telegram)
        │ HTTPS
        ▼
  Telegram Bot API  ◄── outbound long-poll, no inbound ports ──┐
                                                               │
┌──────────────────────────────────────────────────────────────┼───┐
│ Raspberry Pi (always on)                                     │   │
│   ┌──────────────┐          MQTT          ┌───────────┐      │   │
│   │ pcwake-hub   │◄──────────────────────►│ Mosquitto │      │   │
│   │  bot + WoL   │                        │  :1883    │      │   │
│   └──────┬───────┘                        └─────▲─────┘      │   │
└──────────┼──────────────────────────────────────┼────────────┴───┘
           │ magic packet                         │ MQTT, outbound
           │ UDP :9 broadcast                     │ from the PC
           ▼                                      │
      ┌────────────────────────────────────────────┴──┐
      │ PC — pcwake-agent (Windows or Linux)          │
      └───────────────────────────────────────────────┘
```

Every connection is outbound. No port forwarding, no dynamic DNS, no
password to invent — Telegram carries the app, the notifications and the
account security, and a chat-id allowlist decides who may issue commands.

## What it does

| | |
|---|---|
| **Wake** | Magic packet from the Pi, then it watches for the PC to appear |
| **Status** | Online / offline — and *powered on but the agent is dead*, which is a different problem |
| **Power** | Sleep, shut down, restart, lock. Shut down and restart ask first |

**Remote desktop is deliberately not here.** `/remote` hands off to RustDesk
or Moonlight, which already do it well.

## Status, more precisely

Presence alone would conflate "the PC is off" with "the agent crashed". The
hub folds in a ping to tell them apart:

| MQTT presence | Ping | Shown |
|---|---|---|
| online | — | 🟢 Online |
| offline | no reply | ⚫ Offline |
| offline | replies | 🟡 Powered on, agent not running |
| a wake is in flight | — | 🔵 Waking… |

The agent's connection carries an MQTT *last will*, so the broker announces
the PC as offline when the connection dies — including on a yanked power
cable. Nothing polls for this.

## Install

Full walkthrough in **[docs/SETUP.md](docs/SETUP.md)**; when something does
not work, **[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)** is ordered
by how often each cause is the real one.

On the Pi:

```bash
sudo apt install mosquitto mosquitto-clients
pip install ".[hub]"
```

On the PC:

```bash
pip install ".[agent]"
python -m pcwake.agent doctor    # checks the settings that silently break waking
```

`doctor` is worth running before anything else. Almost every "Wake-on-LAN
doesn't work" turns out to be Windows Fast Startup or an unarmed NIC, and it
names both.

## First run

Leave `dry_run = true` in the agent config to begin with. The agent then logs
what it *would* do and acknowledges normally, so you can exercise the whole
path — broker, protocol, correlation, acking — without suspending anything.
`/status` flags a dry-run agent so you are never confused about why nothing
happened.

## Development

```bash
pip install -e ".[dev]"
pytest
```

The integration tests start a real Mosquitto on a scratch port rather than
mocking one, because the behaviour that matters — retained messages, last
wills, QoS 1 publish confirmation — belongs to the broker. They skip
automatically if `mosquitto` is not installed.

## Not in v1

Named scripts, CPU/RAM/disk metrics, file push and clipboard sync are v2, and
deliberately have no scaffolding here yet. Remote desktop streaming is out of
scope permanently.

## Licence

MIT.
