# Public dashboard: HTTPS and a login, no VPN

Puts a read-only dashboard (any web page this box already serves on a private address) on the public
internet so you can open it from a browser on any machine: no VPN client, no tunnel or proxy service. The
reverse proxy ([Caddy](https://caddyserver.com)) runs on the same server, so traffic goes from your browser
straight to that server. A free DNS name only *resolves* to the server; it never carries traffic.

```text
browser --HTTPS (Let's Encrypt cert)--> Caddy on this server --HTTP--> the dashboard (unchanged)
                login required
```

Nothing here is installed by `setup.sh`. The script is called `expose.sh`, not `install.sh`, on purpose:
`setup.sh` runs every `<dir>/install.sh` on every setup, and publishing a dashboard must be a decision you
make by hand.

## Before you run it

- The dashboard must be **read-only and free of secrets**. The login protects it; it does not make it safe
  to publish a page that leaks a key. Look through the files first, for real keys and tokens (not just the
  words "password" or "api_key" appearing in prose).
- Ports 80 and 443 must be free on the server's public address (80 for the certificate check and the
  redirect to HTTPS). If your provider has a cloud firewall in front of the server, open both there.
- Ubuntu or Debian with `apt`, and `sudo`.

## Run it

```bash
sudo PD_UPSTREAM=http://127.0.0.1:8787 PD_USER=me ./public-dashboard/expose.sh
```

`PD_UPSTREAM` is the address the dashboard listens on now. It is only read at setup and written to the
host's own Caddyfile; this repository never holds it. The script:

1. installs Caddy from the official Caddy apt repository (if it is not installed);
2. generates a 40-character random password, keeps it in `/etc/caddy/dashboard.credentials` (mode 600,
   root only) and writes only its bcrypt hash into `/etc/caddy/Caddyfile`;
3. renders [`Caddyfile.template`](Caddyfile.template) into `/etc/caddy/Caddyfile`, validates it, and starts
   Caddy (enabled at boot, restarted after a crash);
4. waits until HTTPS answers with a valid Let's Encrypt certificate, then prints the URL.

Read the password with `sudo cat /etc/caddy/dashboard.credentials` and hand it over privately. Re-running
is safe: the existing login is kept and an unchanged config is not touched.

### Settings

| Variable | Default | Meaning |
|---|---|---|
| `PD_UPSTREAM` | `http://127.0.0.1:8787` | the dashboard to put in front |
| `PD_USER` | `dashboard` | the one login name |
| `PD_HOSTNAME` | `<public-ip-with-dashes>.sslip.io` | public name. [sslip.io](https://sslip.io) answers a name made of an IP with dashes (a-b-c-d.sslip.io) with that IP (a.b.c.d); it needs no account |
| `PD_BIND` | address of the default route | the address Caddy listens on (never the wildcard, see below) |
| `PD_PUBLIC_IP` | `PD_BIND` if public, else asked of api.ipify.org once | only used to build `PD_HOSTNAME` |
| `PD_ACME` | `production` | `staging` for a dry run with an untrusted test certificate, or a directory URL |
| `PD_WAIT` | `180` | seconds to wait for the certificate |

Your own domain works too: point an A record at the server and set `PD_HOSTNAME=dash.example.com`.

If sslip.io ever hits a Let's Encrypt rate limit, use a DuckDNS subdomain (free, needs a token from you) as
`PD_HOSTNAME`, or run once with `PD_ACME=staging` to confirm the rest works while you wait.

## Prove it works

```bash
sudo PD_CHECK_PATHS="scores.json other/page.html" ./public-dashboard/expose.sh check
```

From the server itself, with the real name and the real login, it checks: a valid certificate with no login
gets 401; a wrong password gets 401; the right one gets 200 on `/` and on each path in `PD_CHECK_PATHS`; a
deep path without a login gets 401; a POST gets 405; plain HTTP redirects to HTTPS; the bare IP serves
nothing; Caddy is enabled at boot. The password goes to `curl` on stdin, not on the command line.

`./public-dashboard/test.sh` is the offline test (no root, no network): it renders the template, validates it
with Caddy when present, and checks that nothing in this directory carries an address, hostname or secret.

## Day to day

| Want to | Run |
|---|---|
| change the password | `sudo ./public-dashboard/expose.sh rotate` (the old one stops working at once) |
| take it off the internet | `sudo ./public-dashboard/expose.sh remove` (stops and disables Caddy, deletes the credentials) |
| see what Caddy did | `journalctl -u caddy` (request logs hold no passwords) |
| see the config | `sudo cat /etc/caddy/Caddyfile` |

## Design notes

- **One address, not the wildcard.** Caddy binds ports 80 and 443 on one address only (`default_bind`).
  Tailscale's own `:443` listener on the tailnet address would make a wildcard bind fail, and this way the
  private path keeps working untouched.
- **The login is the only way in.** `basic_auth` covers every path of the site. A request to the bare IP, or
  with another `Host`, matches no site and gets no content. There is no unauthenticated route.
- **Writes are refused at the edge** (405 for anything but GET and HEAD) even though the dashboard
  server is read-only already.
- **No lockout.** Caddy's `basic_auth` has no failed-attempt limit. A 40-character random password cannot be
  guessed at network speed; if you want a lockout as well, add `fail2ban` on the Caddy log.
- **Browsers cache nothing** (`Cache-Control: no-store`) and the site asks search engines to ignore it.
- **The upstream is unchanged.** Whatever serves the dashboard now keeps running as it is, on its private
  address, for anyone who can still reach it.
- **Rebooting.** Caddy is a normal enabled systemd service ordered after `network-online.target`. The
  certificate lives in `/var/lib/caddy` and is renewed by Caddy on its own.
