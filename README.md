# RedhAPI

RedhAPI tells Redh Control, the app for iPhone, Apple Watch and Mac
([redh-control](https://github.com/BooneyNoobington/redh-control)), whether its server is up, and
what the machine is doing: its processor, temperature, power, memory and disks. It shuts the server
down on request. On a server with [redh-manager](https://github.com/BooneyNoobington/redh-manager),
it also lists the apps. As the rooster of other servers, it wakes them up with a Wake-on-LAN packet.
Every server runs one, as an unprivileged systemd service: behind Caddy, with a certificate from
your own CA, or on its own, with a certificate of its own that Redh Control pins:

```
iPhone / Watch / Mac ──HTTPS──▶ Caddy, rapi.SERVER.internal ──HTTP──▶ RedhAPI, 127.0.0.1:8470
                     └─HTTPS, RedhAPI's own certificate─────────────────▶ RedhAPI, SERVER:8470
                                                                         │ sudo, two rules
                                                                         ▼
                                                       /usr/local/bin/redh-shut, as root
                                                       /usr/local/bin/redh list --json, as root

RedhAPI ──UDP broadcast──▶ the magic packet that wakes another server
```

It needs Python 3.9 or newer and nothing but the standard library; for its own certificate,
openssl once. The shutdown itself comes from your own `redh-shut`.

## API

The base URL is `https://rapi.SERVER.internal` behind Caddy, or `https://SERVER:8470`, with any
name or address of the server, where RedhAPI serves HTTPS itself. Everything but `/health` needs
the server's token, as `Authorization: Bearer <token>`. Answers are JSON and never cached.
Unknown paths, other methods and query parameters are errors, not ignored.

### `GET /health`

No token needed. While RedhAPI runs: `200 {"status": "ok"}`.

### `GET /v1/status`

```json
{
  "name": "SERVER",
  "version": "0.2.0",
  "boot_id": "10ae18e3-9be7-42df-a9d2-9ecd0c34bf0d",
  "uptime_seconds": 273813,
  "shutdown": {
    "ready": true,
    "problem": null,
    "pending": false,
    "last_error": null
  },
  "wake": {
    "broadcast": "255.255.255.255",
    "port": 9
  }
}
```

| Field | Meaning |
|---|---|
| `name` | `name` from the configuration, or else the host name |
| `version` | RedhAPI's version |
| `boot_id` | The kernel's ID for this boot. It changes with every start, so a reboot shows. |
| `uptime_seconds` | Whole seconds since boot |
| `shutdown.ready` | Whether a shutdown request would be carried out: redh-shut exists, only root can change it, and sudo lets RedhAPI run it |
| `shutdown.problem` | Why not, when `ready` is false, such as `/usr/local/bin/redh-shut does not exist` |
| `shutdown.pending` | A shutdown was accepted, and redh-shut hasn't finished yet |
| `shutdown.last_error` | Why the last shutdown failed, if it did; cleared by the next request |
| `wake.broadcast`, `wake.port` | Where `POST /v1/wake` sends its magic packets, from `[wake]` in the configuration. Versions before 0.2.0 have no `wake` and can't wake servers. |

`boot_id` and `uptime_seconds` are `null` where there is no `/proc`.

### `POST /v1/shutdown`

No parameters: an empty body, or `{}`. The answer comes first, `202 {"accepted": true,
"delay_seconds": 2}`, and redh-shut runs `delay_seconds` later, so the answer reaches the app
before the server goes down. A second request while one is pending is accepted too, but
redh-shut runs only once. When shutdown isn't ready, the answer is `409` with the code
`shutdown_unavailable` and the problem as its message.

### `POST /v1/wake`

Wakes another server up, for which this one is the rooster: RedhAPI sends the Wake-on-LAN magic
packet for the other server's network card. The body names the card's MAC address, with colons or
hyphens: `{"mac": "AA:BB:CC:DD:EE:FF"}`. Any valid token may wake any address. The packet can't do
harm, and the same token may shut this server down.

The packet goes out as a UDP broadcast to `broadcast` and `port` from `[wake]`, by default
`255.255.255.255` and port 9, which leaves the choice of network to the kernel. A server in
several networks needs the broadcast address of the one the sleeping server is in, such as
`192.168.1.255`. The answer is `200 {"sent": true, "mac": "AA:BB:CC:DD:EE:FF"}`. Sent doesn't
mean woken: the other server is up once its own RedhAPI answers.

A body without a MAC address is `400 bad_request`, such as `'nas' is not a MAC address such as
AA:BB:CC:DD:EE:FF.` When the packet can't be sent, the answer is `409` with the code
`wake_failed` and the reason as its message.

### `GET /v1/apps`

The apps on a server with redh-manager, as `redh list --json` writes them, unchanged; redh-manager's
README describes the fields. RedhAPI runs exactly `sudo -n /usr/local/bin/redh list --json`, which
the second sudo rule allows, and keeps the answer for 10 seconds, since redh asks systemd and
Podman each time:

```json
{
  "root": "/opt/podman",
  "apps": [
    {
      "name": "nextcloud",
      "layout": "quadlet/",
      "units": 3,
      "services": 2,
      "active": 2,
      "running": 1,
      "installed": "in sync",
      "caddy": 1
    }
  ],
  "containers": {
    "running": 2,
    "in_no_app": [
      {
        "name": "scratch",
        "image": "docker.io/library/busybox:1",
        "unit": null
      }
    ]
  },
  "warnings": []
}
```

Without redh-manager, without its sudo rule, or when redh fails, the answer is `409` with the code
`apps_unavailable` and the reason as its message, such as `/usr/local/bin/redh does not exist;
redh-manager isn't installed`. RedhAPI looks at nothing of the apps itself: they belong to the
podman user, below a directory only root and podman may enter.

### `GET /v1/system`

What the machine is doing, so that a server's page in Redh Control has something to show, with
redh-manager or without. RedhAPI reads it from `/proc` and `/sys` as its own unprivileged user;
whatever it can't read is `null`:

```json
{
  "cpu": {
    "count": 8,
    "usage_percent": 7.3,
    "load": [0.42, 0.38, 0.35],
    "pressure_percent": 1.25
  },
  "temperature": {
    "celsius": 46.0,
    "sensor": "coretemp: Package id 0"
  },
  "power": {
    "watts": 12.4,
    "sensor": "package-0",
    "problem": null
  },
  "memory": {
    "total_bytes": 16384000000,
    "available_bytes": 9216000000,
    "pressure_percent": 0.0
  },
  "swap": {
    "total_bytes": 4096000000,
    "free_bytes": 3584000000
  },
  "disks": [
    {
      "path": "/",
      "total_bytes": 249999998976,
      "free_bytes": 180999999488
    },
    {
      "path": "/srv/data",
      "total_bytes": 4000000000000,
      "free_bytes": 1619999997952
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `cpu.count` | The processors the kernel counts, threads included |
| `cpu.usage_percent` | How busy they were together since the previous reading; waiting for I/O counts as idle |
| `cpu.load` | The load averages of the last 1, 5 and 15 minutes |
| `cpu.pressure_percent`, `memory.pressure_percent` | The share of the last 10 seconds in which tasks waited for the processor, or for memory: `some avg10` of the kernel's pressure stall information, `/proc/pressure` |
| `temperature` | The processor's: from its hwmon driver (coretemp, k10temp, zenpower, cpu_thermal and the like), its main sensor or else its hottest; else from a thermal zone; and the sensor's name |
| `power` | The processor's power, not the whole machine's: from the energy counters (RAPL) of its packages, which it names. `problem` says why there is none. |
| `memory`, `swap` | `MemTotal`, `MemAvailable`, `SwapTotal` and `SwapFree` from `/proc/meminfo`, in bytes |
| `disks` | For each path in `disks` under `[system]`, only `/` by default: the size of its file system, and what's free on it for anyone but root |

Usage and power need two readings, the change from one to the next. RedhAPI keeps a reading for 5
seconds, so these numbers change at most that often. The first request after a start, or one more
than a minute after the last reading, waits a second for a second reading.

**The power** comes from `/sys/class/powercap/intel-rapl:*/energy_uj`, also on AMD's processors.
Since Linux 5.10 only root may read these counters, because fast readings of them leak what the
processor does. Without them, `power.watts` is `null`, and `redh-api check` says so. The udev rule
[deploy/redh-api.rules](deploy/redh-api.rules) gives them to RedhAPI's group alone, whenever the
kernel adds one; RedhAPI reads them at most every 5 seconds. It's optional:

```sh
sudo install -m 644 deploy/redh-api.rules /etc/udev/rules.d/90-redh-api.rules
sudo udevadm control --reload
sudo udevadm trigger --action=add --subsystem-match=powercap
```

Machines without these counters, such as a Raspberry Pi, report no power.

### Errors

All errors look like `{"error": {"code": "...", "message": "..."}}`.

| Status | Code | When |
|---|---|---|
| 400 | `unexpected_parameters` | Query parameters, a shutdown body other than `{}`, or a wake-up body with more than `mac` |
| 400 | `bad_request` | A path that can't be parsed, a `Content-Length` that isn't a number, or a wake-up without a MAC address |
| 401 | `unauthorized` | No token, or one not in `[tokens]`; with `WWW-Authenticate: Bearer` |
| 404 | `not_found` | No such endpoint |
| 405 | `method_not_allowed` | The wrong method; `Allow` names the right one |
| 409 | `shutdown_unavailable` | Shutdown isn't ready; the message says why |
| 409 | `apps_unavailable` | The apps can't be listed; the message says why |
| 409 | `wake_failed` | The magic packet couldn't be sent; the message says why |
| 411 | `length_required` | A chunked body |
| 413 | `too_large` | A body over 1024 bytes |
| 500 | `internal` | A bug; the journal has the traceback |
| other | `http_<status>` | Errors from Python's HTTP server itself, such as `http_501` for an unknown method |

What a client can conclude: any answer from RedhAPI, even a 401, means the server is up. A 502
comes from Caddy and means the server is up but RedhAPI isn't running. Without any connection,
the server is down or unreachable, and Redh Control asks the FRITZ!Box, where one is the server's
rooster.

## Installing it on a server

The commands run in a clone of this repository, and assume the default settings:

```sh
git clone https://github.com/BooneyNoobington/redh-api.git ~/src/redh-api
cd ~/src/redh-api
```

1. **The command,** with pipx, like redh-manager:
   ```sh
   sudo apt install pipx                    # Arch: sudo pacman -S python-pipx
   sudo pipx install --global .
   redh-api --version
   ```
2. **The user** `redh-api`, a system user without login or home (execute from within the cloned repo):
   
   ```sh
   sudo install -d -m 750 /etc/sysusers.d
   sudo install -m 644 deploy/redh-api.sysusers /etc/sysusers.d/redh-api.conf
   sudo systemd-sysusers
   ```
3. **The configuration:**
   ```sh
   sudo install -d -m 750 -g redh-api /etc/redh-api
   sudo install -m 640 -g redh-api redh-api.conf.example /etc/redh-api/redh-api.conf
   ```
4. **redh-shut and the sudo rules.** redh-shut has to belong to root, and only root may be able to
   change it or any directory above it; otherwise RedhAPI refuses to run it. A server with
   virtual machines has [an example](#servers-with-virtual-machines) to start from.
   ```sh
   sudo install -m 755 redh-shut /usr/local/bin/redh-shut
   EDITOR=vim sudo visudo -f /etc/sudoers.d/redh-api
   ```
   The rules are these three lines, from [deploy/redh-api.sudoers](deploy/redh-api.sudoers):
   ```
   Defaults:redh-api secure_path="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
   redh-api ALL=(root) NOPASSWD: /usr/local/bin/redh-shut ""
   redh-api ALL=(root) NOPASSWD: /usr/local/bin/redh list --json
   ```
   The first gives the commands a fixed `PATH`. Not every system sets `secure_path`, and without
   it sudo passes on the caller's: whoever runs as `redh-api` could hand redh-shut a `systemctl`
   of their own. The `""` means no arguments. The third is for `GET /v1/apps`: redh-manager's
   list, with exactly these arguments; without redh-manager it does nothing. The file name has no
   dot because sudo skips files in `sudoers.d` whose name contains one.
5. **The service:**
   ```sh
   sudo install -m 644 deploy/redh-api.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now redh-api
   ```
6. **TLS,** one of two ways:
   - **Behind Caddy, with your own CA:** put [deploy/redh-api.caddy](deploy/redh-api.caddy), with
     `SERVER` replaced, into the directory the main Caddyfile imports (`/etc/caddy/Caddyfile.d/`
     where redh-manager runs). Then `sudo systemctl reload caddy`; if the new snippet doesn't
     load, Caddy keeps the configuration it had. In the local DNS, `rapi.SERVER.internal` points
     at the server. Redh Control trusts your CA's root, imported once for all servers.
   - **On its own, with a certificate of its own,** without a CA, Caddy or a DNS name. RedhAPI
     makes a self-signed certificate for ten years, and serves HTTPS itself:
     ```sh
     sudo redh-api certificate
     ```
     It prints the certificate's fingerprint and what goes into the configuration:
     ```
     Made /etc/redh-api/tls.crt for nas, valid for ten years, and its key /etc/redh-api/tls.key.
     Its SHA-256 fingerprint, which the pairing codes carry:
         02:41:27:D9:1D:BB:F6:9E:EA:BE:71:82:A4:2A:9A:AE:B4:D8:74:60:9F:7D:19:20:C9:6F:C0:5E:06:C6:08:FA

     For /etc/redh-api/redh-api.conf, so that RedhAPI serves HTTPS itself on every address of this server,
     without Caddy; restart redh-api afterwards:
         [api]
         listen = 0.0.0.0:8470
         [tls]
         certificate = /etc/redh-api/tls.crt
         key = /etc/redh-api/tls.key
     ```
     Then `sudo systemctl restart redh-api`, and open port 8470 in the server's firewall if it
     has one. Redh Control trusts exactly this certificate, through the pairing code of the next
     step, and reaches RedhAPI by any name or address of the server, such as
     `https://192.168.1.10:8470`. A new certificate would break every pairing, so the command
     keeps an existing one.
7. **A token for each device,** which goes into Redh Control:
   ```sh
   redh-api token redh-control
   EDITOR=vim sudoedit /etc/redh-api/redh-api.conf     # the printed line goes under [tokens]
   sudo systemctl restart redh-api
   ```
   Nothing keeps a copy: the server knows only its hash. With RedhAPI's own certificate, it also
   prints a pairing code, which goes into the app instead of the token:
   ```
   Its pairing code, for Redh Control: the token and this server's certificate in one, to paste
   instead of the token:
       redh1:024127d91dbbf69eeabe7182a42a9aaeb4d874609f7d1920c96fc05e06c608fa:am7zjEPH9VIWSAtbXxBu18SlWSojCFM9dM4KK7abbns
   ```
   The code is `redh1:`, the certificate's SHA-256 in hex, `:` and the token: keep it as secret as
   the token.
8. **The check:**
   ```sh
   sudo redh-api check
   curl https://rapi.SERVER.internal/health     # behind Caddy
   ```
   Run as root, `check` reads the configuration and asks sudo about the user `redh-api`. On a
   finished server behind Caddy, it prints:
   ```
   ok  config   /etc/redh-api/redh-api.conf, tokens: redh-control
   ok  command  /usr/local/bin/redh-shut belongs to root, and only root can change it
   ok  sudo     redh-api may run /usr/local/bin/redh-shut as root
   ok  apps     redh-api may run /usr/local/bin/redh list --json as root
   ok  system   temperature from coretemp: Package id 0; power from package-0
   ok  service  answers on 127.0.0.1:8470
   ```
   With RedhAPI's own certificate, a line about it follows the first, and the last one asks over
   HTTPS, trusting exactly that certificate:
   ```
   ok  tls      its own certificate, 02:41:27:D9:1D:BB:F6:9E:EA:BE:71:82:A4:2A:9A:AE:B4:D8:74:60:9F:7D:19:20:C9:6F:C0:5E:06:C6:08:FA
   ```
   Lines starting with `!!` say what's missing, and the exit status is 1. The apps are optional:
   without redh-manager or its rule, their line starts with `--` and says why they aren't offered.
   So are the sensors of `GET /v1/system`: the system line says which it found, and starts with
   `--` where the temperature or the power is missing.

To try the whole chain without shutting anything down, start with a redh-shut that only writes to
the journal:

```sh
#!/bin/sh
logger -t redh-shut "a shutdown was requested"
```

## What redh-shut gets

- It runs as root through sudo, without arguments, with its input from `/dev/null` and the `PATH`
  from the sudo rule.
- Its output goes to the journal of `redh-api.service` (`journalctl -u redh-api`). After a failed
  run, its last line on stderr is also in `shutdown.last_error`.
- Exit status 0 means the shutdown is on its way. Anything else counts as a failure, and the
  server stays up.
- It runs inside the service's cgroup, so stopping redh-api stops redh-shut too, and a shutdown
  stops redh-api. So whatever takes a while, a backup for instance, comes first, and
  `systemctl poweroff` comes last.

## Servers with virtual machines

Powering off a server would cut its virtual machines off.
[examples/redh-shut-libvirt](examples/redh-shut-libvirt) is a redh-shut for libvirt that shuts
them down first:

- It asks every running guest to shut down, and powers the server off once all of them are off.
  Meanwhile `shutdown.pending` is true, and the app says "Shutting down".
- A guest still running after two minutes keeps the server up. redh-shut fails, and the app shows
  the reason, such as `GUEST didn't shut down within 120 seconds`. No guest is destroyed. The two
  minutes leave room within the three that the app waits for a server to go down.

```sh
sudo install -m 755 examples/redh-shut-libvirt /usr/local/bin/redh-shut
```

Before relying on it, check each guest, with `GUEST` its name in `sudo virsh list`:

1. **`sudo virsh shutdown GUEST` shuts it down.** virsh asks the guest's QEMU guest agent if the
   guest has the agent's channel, and otherwise presses the virtual power button, which some
   guests ignore. Home Assistant OS comes with the agent. To give a guest the channel:
   ```sh
   cat > guest-agent.xml <<'EOF'
   <channel type='unix'>
     <target type='virtio' name='org.qemu.guest_agent.0'/>
   </channel>
   EOF
   sudo virsh attach-device GUEST guest-agent.xml --config
   ```
   The channel is there once the guest has been stopped and started again; a reboot from inside
   isn't enough. Then `sudo virsh qemu-agent-command GUEST '{"execute":"guest-ping"}'` answers
   `{"return":{}}`.
2. **It starts with the server:** `sudo virsh autostart GUEST`. Otherwise it stays off after a
   wake-up: redh-shut leaves no guest running for libvirt-guests to bring back.

## Why the service isn't sandboxed further

[deploy/redh-api.service](deploy/redh-api.service) runs RedhAPI as its own user and leaves it at
that. redh-shut is a child of the service and inherits its sandbox. `NoNewPrivileges=`, and every
option that implies it (`SystemCallFilter=`, `RestrictSUIDSGID=`, `PrivateDevices=`,
`ProtectKernelTunables=` and more), stops sudo from working. `ProtectSystem=`, `ProtectHome=`
and the like would apply to redh-shut as well. `PrivateNetwork=` and `IPAddressDeny=` would keep
the magic packets of `POST /v1/wake` in. [tests/test_deploy.py](tests/test_deploy.py) keeps the
unit that way.

The protection is elsewhere instead:
- Behind Caddy, RedhAPI listens on loopback only. On its own, it speaks nothing but HTTPS, TLS 1.2
  or newer, with the certificate that the app pins.
- Every endpoint but `/health` needs a 256-bit token.
- The sudo rules allow two commands with fixed arguments, redh-shut with none and
  `redh list --json`, and give them a fixed `PATH`.
- RedhAPI runs neither unless only root can change it.

## Updating

```sh
git -C ~/src/redh-api pull
sudo pipx install --global --force ~/src/redh-api
sudo systemctl restart redh-api
```

Changed tokens need the restart as well. Changes to the files in `deploy/` don't arrive this way:
install them again as in the steps above.

## Development

```sh
python3 -m unittest discover -s tests             # the tests, on the newest Python
/usr/bin/python3 -m unittest discover -s tests    # and on macOS's 3.9, the oldest supported
```

To try it locally, with a configuration that `.gitignore` keeps out of git:

```sh
cp redh-api.conf.example dev.conf
python3 -m redh_api token dev                     # add the printed line to dev.conf
python3 -m redh_api serve --config dev.conf
curl -H 'Authorization: Bearer TOKEN' http://127.0.0.1:8470/v1/status
```

On macOS, shutdown isn't ready because there's no redh-shut, and `boot_id` and `uptime_seconds`
are `null`.

## Licence

GPL-3.0, see `LICENSE`.
