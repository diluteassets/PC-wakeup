# Roadmap

What is built, what is next, and the decisions behind both — recorded here
because they were made in conversation and would otherwise be lost.

This is a record of decisions already taken. It deliberately does **not**
design v2, because v2 is gated (see below) and designing it now would mean
committing to answers before the questions have been asked in anger.

---

## The constraint everything follows from

**When the PC is off, nothing on the PC can listen.**

So something else on the LAN has to send the Wake-on-LAN magic packet. That
single fact decides the shape of the whole project: there are two pieces, not
one, and the always-on piece is where the wake lives. Every other choice
below is downstream of it.

The always-on device is a **Raspberry Pi**. It runs the MQTT broker, the
Telegram bot, and the magic-packet sender.

| Decision | Choice | Why |
|---|---|---|
| Always-on device | Raspberry Pi | Runs broker + bot + WoL in one place; room for v2 |
| PC support | Windows and Linux | Platform layer from day one, so no rewrite later |
| Transport | Mosquitto on the Pi | MQTT last-will gives offline detection for free |
| Phone UI | Telegram bot | No app to build, no inbound ports, no auth to write |

---

## Status

**v1 is built and pushed. It has not yet run on real hardware.**

322 tests pass, and the system has been verified end to end against a real
broker, a real agent process and real `ping` — including a cold start, a
broker restart, and the agent-down state. What has *not* been exercised is
anything needing the physical machines: BIOS Wake-on-LAN, a genuine cold
power-off and wake, the Windows `ctypes` calls on real Windows, and a real
Telegram bot token.

---

## v1 — done

- **Wake** — magic packet from the Pi, then it watches for the PC to appear.
- **Status** — online / offline, plus *powered on but the agent is dead*,
  which is a different problem and would otherwise look identical to off.
- **Power** — sleep, shut down, restart, lock. The destructive two ask first.

---

## The gate

> **v2 starts only once v1 has run for a week.**

Not a formality. A week of real use is the only way to find the failure modes
no test can simulate: a Pi that reboots at 3am, a router that ages the PC's
MAC out of its table after a long power-off, a Windows update that quietly
re-enables Fast Startup. Building v2 on top of a v1 that has not survived
that is building on an unknown.

Before starting v2, confirm all of:

- [ ] `python -m pcwake.agent doctor` reports no failures on the real PC
- [ ] Wake from a genuine cold power-off works, repeatedly
- [ ] Both services survive a reboot of the Pi and of the PC
- [ ] A week has passed with no unexplained status or command failures

---

## v2 — not started, no scaffolding

Three features, in the order they are worth building:

1. **Run a fixed list of named scripts.** Named, *not* arbitrary commands.
   The whole point is that the set of things the bot can do to the PC stays
   enumerable and reviewable. A bot that can run anything is a remote shell
   with a chat interface, and it only takes one leaked token to regret that.
2. **CPU / RAM / disk / temperatures.**
3. **Push a file, sync clipboard.**

There is no code, no config key, and no protocol field for any of these yet —
on purpose. Speculative scaffolding for features that have not been designed
tends to be wrong in ways that are expensive to unpick.

---

## Permanently out of scope

**Remote desktop streaming.** Not "later" — not at all. RustDesk and
Moonlight already do it properly, and `/remote` deep-links to them. Competing
with them would be the single biggest sink of effort in the project for the
smallest gain.

---

## Constraints any v2 work has to respect

These come out of v1's design and its verification. Breaking one is a
deliberate decision, not an oversight.

- **The action set is closed.** `protocol.Action` is exactly `sleep`,
  `shutdown`, `restart`, `lock`. Adding to it is a protocol change affecting
  both halves. Named scripts must not become a hole in this — a script id
  from a fixed, agent-side list is a closed set; a command string is not.
- **The wire protocol is one shared file.** `common/protocol.py` is imported
  by both halves so they cannot drift. Keep it that way.
- **Acks for host-downing actions are sent before the action runs.** They
  have to be: the machine is gone before an after-the-fact ack could leave
  the wire. So such an ack means *accepted*, never *completed* — the status
  transition is what confirms the rest. Any new action that ends the
  connection inherits this, and `Action.takes_host_down` is how it is
  declared.
- **The ACL confines each agent to its own topics.** A compromised agent can
  talk about itself and nothing more; it cannot issue commands or forge
  another host's status. New topics need matching ACL entries, and
  `tests/test_acl.py` will fail if the confinement breaks.
- **Every command ends in a visible outcome.** Acknowledged, refused, or
  timed out — never silence. A command that quietly does nothing is the one
  failure that would make the whole thing untrustworthy.
- **Config over convention.** Secrets can come from the environment so they
  need never be written to disk.

Two practical notes that cost real time to discover:

- **On Windows there is no console.** The agent runs under `pythonw.exe`, so
  anything written to stderr is discarded. Use the logger; it writes to a
  file. See `common/logging.py`.
- **Host names appear in three places** — the agent's `[agent].host`, the
  hub's `[[hosts]]`, and the mosquitto ACL. They must match, and two PCs
  sharing a name makes them fight over one MQTT client id forever.

---

## If you are picking this up cold

Read `README.md` for the shape of the system, `docs/SETUP.md` to install it,
and `docs/TROUBLESHOOTING.md` when it misbehaves — that one is ordered by how
often each cause is actually to blame. The commit history carries the
reasoning behind most of the non-obvious code, particularly the ack ordering
in `agent/client.py` and the status model in `hub/state.py`.
