# RedhAPI: orientation for Claude sessions

RedhAPI runs on every server of the homelab. It tells Redh Control, the app for iPhone, Apple
Watch and Mac, whether the server is up and what the machine is doing, shuts the server down on
request, lists the apps where redh-manager runs them, and wakes other servers up as their rooster,
with a Wake-on-LAN packet. [README.md](README.md) is the reference: the API contract, installing,
redh-shut and the sandboxing.

Redh Control is a repository of its own, `redh-control`, checked out next to this one on the
MacBook (`../redh-control`). Its Swift package RedhKit is the client of the contract below. All
three are public on GitHub, under GPL-3.0-only: `BooneyNoobington/redh-api` (this one; the
checkout's folder is still `redhapi`), `redh-control` and `redh-manager`.

| Path | What |
|---|---|
| `redh_api/` | The service: `config.py`, `shutdown.py` (sudo and redh-shut), `apps.py` (`redh list --json`), `wake.py` (the magic packet, as a UDP broadcast), `system.py` (the boot, and `GET /v1/system` from `/proc` and `/sys`), `tls.py` (its own certificate, the pairing code), `server.py` (the HTTP API), `cli.py` (`serve`, `check`, `token`, `certificate`) |
| `deploy/` | The systemd unit, the sysusers file, the sudo rules, the Caddy snippet, and the optional udev rule for the energy counters |
| `examples/` | A redh-shut for servers with virtual machines under libvirt |
| `tests/` | unittest, standard library only |

## Ground rules

1. **Nothing real goes into the repository,** which is public: no host names, IP addresses, tokens
   or users of the homelab. Use placeholders such as `rapi.SERVER.internal`. The real
   configuration lives on the servers.
2. **Python 3.9 or newer, standard library only;** openssl only to make RedhAPI's own certificate.
3. **TLS, one of two ways, never plain HTTP on the network:**
   - behind Caddy, RedhAPI listens on loopback only, and Caddy terminates TLS with a certificate
     from the homelab's ACME CA;
   - with `[tls]`, RedhAPI serves HTTPS itself, on any address, with a self-signed certificate of
     its own that Redh Control pins: the pairing code of `redh-api token` carries its SHA-256.
     A new certificate breaks every pairing, so `redh-api certificate` never replaces one.
4. **Root through sudo only, for two commands:**
   - RedhAPI answers a shutdown request first and runs redh-shut after a short delay, so the
     answer gets out.
   - The sudo rules allow `/usr/local/bin/redh-shut` without arguments and redh-manager's
     `/usr/local/bin/redh list --json`, both with a fixed `PATH`. RedhAPI runs neither unless
     nobody but root can change it. The apps are optional: without redh-manager, `GET /v1/apps`
     answers `409 apps_unavailable`, and `check` marks them `--`, not `!!`.
   - Its systemd unit is lightly sandboxed on purpose. redh-shut runs under sudo inside the same
     sandbox, and `NoNewPrivileges=` or any option that implies it breaks sudo.
     `tests/test_deploy.py` checks this.
5. **The API contract is the README's API section.** Redh Control follows it. RedhKit's tests keep
   a copy of the status example, which they compare with this README wherever both repositories
   sit side by side. Change both together.
6. **Every change comes complete:**
   - unit tests, green on the newest Python and on 3.9;
   - shell scripts clean under shellcheck;
   - the README updated where it describes the change, with example output that comes from
     running the code.
7. **The owner deploys and pushes.** Claude doesn't touch the servers and never pushes.

## Commands (MacBook)

| | |
|---|---|
| `python3 -m unittest discover -s tests` | the tests on Homebrew's Python, the newest |
| `/usr/bin/python3 -m unittest discover -s tests` | the same on macOS's Python 3.9, the oldest supported |
| `uvx --from shellcheck-py shellcheck examples/redh-shut-libvirt` | shellcheck, which isn't installed |

## Lessons learned (don't repeat these)

- **sudo** logs every `sudo -l`. RedhAPI keeps sudo's yes for 10 minutes and its no for 30
  seconds, and asks afresh for every shutdown request.
- **sudo** passes the caller's `PATH` on unless `secure_path` is set, and not every system sets
  it. The sudo rule sets it for `redh-api`, so redh-shut may call commands by name.
- **sudoers.d** skips files whose name contains a dot: the rule goes into
  `/etc/sudoers.d/redh-api`.
- **visudo** checks the rule's syntax on the MacBook too: `tests/test_deploy.py` runs
  `visudo -c -f` wherever visudo exists.
- **examples/redh-shut-libvirt** hasn't met a real server yet. Its tests use stand-ins for virsh,
  systemctl, date and sleep, with a pretend clock, under `/bin/sh` and dash. Each step of the wait
  still starts processes, about 4 ms each, so only one test waits out the two minutes.
- **http.server** sends its own errors (a bad request line, an unknown method) through
  `send_error`. RedhAPI overrides it to keep the JSON error format.
- **Logs** never contain tokens: only the token's name from `[tokens]`.
- **The shell is zsh:** an unquoted `$var` isn't split into words. Spell commands out instead of
  `set -- $var`.
- **The energy counters** (RAPL, `intel-rapl:*/energy_uj`, also on AMD) are root's alone since
  Linux 5.10: fast readings leak what the processor does (Platypus). RedhAPI keeps a reading for
  5 seconds, and the optional udev rule gives the counters to its group. A tmpfiles rule isn't
  enough: it may run at boot before the kernel adds the counters.
- **TLS without Caddy:** each connection's handshake runs in its request's thread
  (`Handler.setup`), not in the accept loop, where one slow client would hold up every other.
  Without Caddy, `X-Forwarded-For` is the client's own claim, so the log ignores it. The tests
  make certificates with whatever openssl is on the PATH, Homebrew's or macOS's LibreSSL; the
  two-step EC key (`ecparam`, then `req -x509`) works with both and with the servers' OpenSSL 3.
- **Examples in the README** come from the code: `GET /v1/system`'s from a fake `/proc` and `/sys`
  as in `tests/test_system.py`, with a clock that the sampler's sleep moves on. A sleep that
  doesn't move it makes two readings microseconds apart, and tens of kilowatts.
