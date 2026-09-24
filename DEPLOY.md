# Where to run it: laptop or server

The stack is light (about 1.5 GB of RAM, almost no CPU) and bots decide at most twice a day, so any
machine works. What matters is **being awake**. The trading service only works while it runs:

| Needs the service running | Works while it's down |
|---|---|
| The decisions (10:00 and/or 15:30 New York). A missed check is skipped, never traded twice. | **Stop-loss on Alpaca bots.** It rests at Alpaca as a real stop order (`held at broker` on the bot page). |
| Take-profit, and the drawdown breaker | |
| Stop-loss on the built-in **paper** simulator (`checked by app`) | |

- **Your laptop** is fine for paper trading.
- **A server** is the way to run real money, or paper results you want to trust every day.

The code and the setup are the same on both.

> **Run the bots on one machine only.** Two copies trading the same Alpaca account would both act on
> every signal. Before starting the stack on a server, stop it on the laptop (`docker compose stop`).

---

## Option A: your laptop (macOS)

The US market is open 9:30–16:00 New York time. Bots decide at 15:30, and at 10:00 if set to check after the open. Convert those to
your own time zone. For UTC+4 that's 17:30–00:00 while the US is on summer time (until the first
Sunday of November), and 18:30–01:00 in winter.

1. **Docker Desktop → Settings → General → "Start Docker Desktop when you sign in"**. Every container is
   set to `restart: unless-stopped`, so the whole stack comes back whenever Docker does.
2. During market hours, keep it **plugged in with the lid open**, and stop it from sleeping:
   ```bash
   caffeinate -i
   ```
   It keeps the Mac awake until you press Ctrl+C.
3. Check the bot page header: **Scheduler running** means the bots are working. **Scheduler not ticking**
   means the service stopped. See `docker compose logs trading-service`.

---

## Option B: a server

### 1. Rent a server

- Any Linux VPS: **Ubuntu 24.04 LTS, 2 vCPUs, 4 GB RAM, 40 GB disk**. That costs roughly $5–25/month
  depending on the provider (Hetzner, DigitalOcean, AWS Lightsail, ...).
- The region hardly matters for a once-a-day strategy. US East is closest to the exchange and Alpaca.
- Add your **SSH key** when you create it, so it never needs a password login.

### 2. Secure the basics

```bash
ssh root@SERVER_IP
adduser trader && usermod -aG sudo trader
rsync --archive --chown=trader:trader ~/.ssh /home/trader   # your SSH key works for the new user too
ufw allow OpenSSH && ufw enable                             # firewall: only SSH comes in
```

Then log in as `trader` from now on: `ssh trader@SERVER_IP`.

Check that SSH refuses passwords and root. Both lines should end in `no`:

```bash
sudo sshd -T | grep -E '^(passwordauthentication|permitrootlogin)'
```

If not, set `PasswordAuthentication no` and `PermitRootLogin no` in `/etc/ssh/sshd_config` (and in any
file in `/etc/ssh/sshd_config.d/` that says otherwise), then run `sudo systemctl restart ssh`.

Why the firewall alone isn't enough: Docker's published ports bypass `ufw`. That's why
`docker-compose.yml` binds **every** port to `127.0.0.1`. Nothing in this stack has a login, so none of
it may face the internet. Don't change those bindings to `0.0.0.0`.

### 3. Install Docker

Follow Docker's official guide: https://docs.docker.com/engine/install/ubuntu/. Then:

```bash
sudo usermod -aG docker trader   # log out and back in afterwards
docker compose version           # check it works
```

Docker starts at boot, and the stack's `restart: unless-stopped` brings every container back after a reboot.

### 4. Copy the project

From your laptop. This also copies the `.env` files with your API keys: they're git-ignored, so a
`git clone` wouldn't bring them.

```bash
rsync -av --exclude node_modules --exclude .venv --exclude __pycache__ --exclude dist --exclude backups \
  ~/ClaudeProjects/Trading/ trader@SERVER_IP:trading/
ssh trader@SERVER_IP 'chmod 600 trading/*/.env'   # only your user can read the keys
```

### 5. Bring your history along (optional)

Skip this if you have no bots yet. Otherwise:

1. On the laptop, stop the bots and take a last snapshot:
   ```bash
   docker compose stop trading-service && make backup-trading-db
   ```
2. Copy the snapshot to the server:
   ```bash
   scp backups/trading-<newest>.sql.gz trader@SERVER_IP:trading/
   ```
3. On the server, restore it **before** the trading service starts:
   ```bash
   cd ~/trading
   docker compose up -d --wait trading-db
   gunzip -c trading-<newest>.sql.gz | docker compose exec -T trading-db psql -U trading trading
   ```

### 6. Start it

```bash
cd ~/trading
docker compose up -d --build
docker compose ps                  # everything "Up" (glitchtip-migrate exits once it's done: that's normal)
curl -s localhost:8002/status      # "scheduler_running": true, and a recent "scheduler_last_tick"
```

### 7. Open the dashboard from your laptop

The server accepts nothing from the internet except SSH. Pick one of two private ways in.

**SSH tunnel.** Nothing to install:

```bash
ssh -N -L 8080:localhost:8080 -L 3000:localhost:3000 -L 8082:localhost:8082 trader@SERVER_IP
```

While that runs, http://localhost:8080 on your laptop *is* the server's dashboard. The Langfuse and
GlitchTip links in the Resources menu work too. Ctrl+C closes it. If the laptop's own stack is
running, stop it first (`docker compose stop`), or the ports clash.

**Tailscale.** Also works from your phone, and is free for personal use:

1. Install it on the server and on your devices (https://tailscale.com/download), signed in to the
   same account.
2. On the server:
   ```bash
   sudo tailscale up
   sudo tailscale serve --bg 8080   # if asked, enable HTTPS in the Tailscale admin console
   ```
3. The dashboard is at `https://<server-name>.<your-tailnet>.ts.net`, reachable only from **your**
   Tailscale devices.

Use `tailscale serve`, never `tailscale funnel`: funnel publishes to the whole internet. The Resources
links to Langfuse and GlitchTip point at `localhost`, so use the SSH tunnel for those.

### 8. Daily backups

Run the backup once by hand, to check it works:

```bash
cd ~/trading && ./scripts/backup-trading-db.sh
```

Then schedule it with `crontab -e` and add this line:

```
30 22 * * * cd $HOME/trading && ./scripts/backup-trading-db.sh >> backups/backup.log 2>&1
```

22:30 is in the server's time zone. Servers normally run on UTC (check with `timedatectl`), and 22:30
UTC is after the US close all year. The script keeps 30 days of `backups/trading-*.sql.gz`.

A backup on the same server is lost with the server, so also copy them off now and then, from your laptop:

```bash
rsync -av trader@SERVER_IP:trading/backups/ ~/trading-backups/
```

Your provider's snapshot backups (a few $/month) cover the whole server as well.

To restore a snapshot:
```bash
gunzip -c backups/<file>.sql.gz | docker compose exec -T trading-db psql -U trading trading
```

### 9. Updating

```bash
# laptop: send the new code (same command as step 4)
rsync -av --exclude node_modules --exclude .venv --exclude __pycache__ --exclude dist --exclude backups \
  ~/ClaudeProjects/Trading/ trader@SERVER_IP:trading/
# server: rebuild and restart what changed
ssh trader@SERVER_IP 'cd trading && docker compose up -d --build'
```

A restart is safe at any time: pending orders are reconciled, and "already decided today" lives in the
database. Still, avoid the decision windows (10:00–10:30 and 15:30–16:00 New York) out of habit.

### 10. Keep an eye on it

- **Bot page header:** `Scheduler running`, or `Scheduler not ticking` (look at the logs).
- **Logs:** `docker compose logs -f trading-service`. Every order and stop is also in each bot's audit log.
- **Errors:** set up GlitchTip once (see README), and errors from every service land there.
- **Disk:** check with `df -h`. Old Docker images pile up after rebuilds; `docker image prune` clears them.
