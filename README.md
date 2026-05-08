# Dafabet Tennis Duplicate Monitor

A Python + Playwright bot that watches the live tennis page on **sports.dafabet.com** every 120 seconds, detects when the same real-world match is listed twice under slightly different player name formats, and sends an instant **Telegram alert**.

**Example duplicate it catches:**
```
Match A: Butvilas, Edas     vs  Imamura, Masamichi
Match B: Butvilas, E        vs  Imamura, M
```

---

## Features

- Expands all collapsed league/group sections automatically
- Handles all name formats: `"Lastname, Firstname"`, `"Lastname, F"`, `"Lastname F"`, doubles pairs (`"Player1/Player2"`)
- Multi-strategy similarity model: exact surname + initial matching, fuzzy fallback, doubles support
- **AI analysis layer** powered by **MiniMax-M2.7** — detects duplicates and player conflicts
- False positive filter: opens both match pages to confirm status before alerting
- Saves detailed anomaly reports to `anomaly_reports/`
- Telegram alerts on startup, each duplicate found, and daily heartbeat at 07:00 UTC
- All credentials stored in `.env` — never committed to git

---

## Project structure

```
flashscore/
├── monitor.py        # main script
├── requirements.txt  # Python dependencies
├── .env.example      # credential template (copy to .env and fill in)
├── .env              # your real secrets — git-ignored, never committed
├── anomaly_reports/  # auto-created; detailed per-anomaly investigation reports
└── .gitignore
```

---

## Local setup (Windows / Mac / Linux with display)

### Prerequisites
- Python 3.11 or later
- Git

### Steps

```bash
# 1. Clone the repo
git clone https://github.com/steroidfreak/flashscore.git
cd flashscore

# 2. Install Python dependencies
pip install -r requirements.txt

# 3. Install Chromium browser (used by Playwright)
playwright install chromium

# 4. Create your secrets file
cp .env.example .env
```

Open `.env` in any text editor and fill in your credentials:

```ini
# Primary recipient
TELEGRAM_BOT_TOKEN=123456:ABC-your-token-here
TELEGRAM_CHAT_ID=987654321

# Second recipient (optional — alerts go to both if set)
TELEGRAM_BOT_TOKEN_2=789012:XYZ-second-bot-token
TELEGRAM_CHAT_ID_2=123456789

MINIMAX_API_KEY=your-minimax-api-key-here
```

> **Get your Telegram bot token:** message [@BotFather](https://t.me/BotFather) on Telegram → `/newbot`
> **Get your Telegram chat ID:** message [@userinfobot](https://t.me/userinfobot) on Telegram
> **Get your MiniMax API key:** sign up at [platform.minimax.io](https://platform.minimax.io)

```bash
# 5. Run
python monitor.py
```

A browser window will open (local mode shows the window by default). Telegram will receive a startup message and duplicate alerts as they are found.

---

## Deploy on a DigitalOcean Droplet (VPS)

### Recommended droplet

| Plan | RAM | vCPU | Price | Verdict |
|------|-----|------|-------|---------|
| Basic 512 MB | 512 MB | 1 | $4/mo | Too small — Chromium may be OOM-killed |
| **Basic 1 GB** | **1 GB** | **1** | **$6/mo** | **Minimum — works fine** |
| Basic 2 GB | 2 GB | 1 | $12/mo | Comfortable with headroom |

**OS:** Ubuntu 22.04 LTS or Ubuntu 24.04 LTS

Memory breakdown on 1 GB droplet:

| Component | RAM |
|-----------|-----|
| Ubuntu OS (minimal) | ~200 MB |
| Python process | ~50 MB |
| Chromium (headless, 1 tab) | ~250–350 MB |
| **Total** | **~500–600 MB** |

---

### Step-by-step VPS deployment

#### 1. Create the Droplet

1. Log in to [cloud.digitalocean.com](https://cloud.digitalocean.com)
2. **Create → Droplets**
3. Choose **Ubuntu 22.04 (LTS) x64**
4. Select **Basic → Regular → $6/mo (1 GB RAM)**
5. Add your SSH key or set a root password
6. Click **Create Droplet**

#### 2. SSH into the server

```bash
ssh root@YOUR_DROPLET_IP
```

#### 3. Install system dependencies

```bash
apt update && apt upgrade -y
apt install -y python3 python3-pip python3-venv git
```

#### 4. Clone the repo

```bash
git clone https://github.com/steroidfreak/flashscore.git
cd flashscore
```

#### 5. Set up a Python virtual environment

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

#### 6. Install Chromium and its system libraries

```bash
playwright install chromium
playwright install-deps chromium
```

> This downloads ~400 MB of packages. Only needed once.

#### 7. Create your `.env` file

```bash
cp .env.example .env
nano .env
```

Minimum required content:

```ini
# Primary recipient
TELEGRAM_BOT_TOKEN=123456:ABC-your-token-here
TELEGRAM_CHAT_ID=987654321

# Second recipient (optional)
TELEGRAM_BOT_TOKEN_2=789012:XYZ-second-bot-token
TELEGRAM_CHAT_ID_2=123456789

MINIMAX_API_KEY=your-minimax-api-key-here
```

`HEADLESS` defaults to `true` on any VPS — no need to set it manually.

Optional overrides (add to `.env` if needed):

```ini
HEADLESS=true              # always true on VPS (no display)
CHECK_INTERVAL=120         # seconds between polls (default: 120)
SIMILARITY_THRESHOLD=0.75  # duplicate detection sensitivity (lower = more alerts)
MIN_SIDE_SCORE=0.60        # both sides must exceed this to flag a duplicate
AI_ANALYSIS=true           # set to false to disable MiniMax AI layer
```

Save and exit (`Ctrl+O`, `Enter`, `Ctrl+X` in nano).

#### 8. Test run

```bash
source venv/bin/activate   # if not already active
python monitor.py
```

You should see a Telegram message: `🟢 Tennis duplicate monitor starting…`. Press `Ctrl+C` to stop.

---

### Keep it running with systemd (recommended)

Systemd automatically restarts the monitor if it crashes or the server reboots.

#### Create the service file

```bash
nano /etc/systemd/system/tennis-monitor.service
```

Paste:

```ini
[Unit]
Description=Dafabet Tennis Duplicate Monitor
After=network-online.target
Wants=network-online.target

[Service]
User=root
WorkingDirectory=/root/flashscore
EnvironmentFile=/root/flashscore/.env
ExecStart=/root/flashscore/venv/bin/python monitor.py
Restart=always
RestartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Save and exit (`Ctrl+O`, `Enter`, `Ctrl+X`).

#### Enable and start the service

```bash
systemctl daemon-reload
systemctl enable tennis-monitor    # auto-start on every reboot
systemctl start  tennis-monitor    # start right now
```

#### Useful commands

```bash
# Check current status
systemctl status tennis-monitor

# Watch live logs
journalctl -u tennis-monitor -f

# Stop the monitor
systemctl stop tennis-monitor

# Restart after editing .env or monitor.py
systemctl restart tennis-monitor
```

---

### Alternative: keep it running with screen

If you prefer something simpler than systemd:

```bash
apt install -y screen

# Start a named session
screen -S monitor

# Activate venv and run
source /root/flashscore/venv/bin/activate
cd /root/flashscore
python monitor.py

# Detach (monitor keeps running): Ctrl+A, then D

# Reattach later
screen -r monitor
```

---

### Updating the bot

```bash
cd /root/flashscore
git pull                           # get latest code
source venv/bin/activate
pip install -r requirements.txt    # in case dependencies changed
systemctl restart tennis-monitor   # apply changes
```

---

## Configuration reference

All settings live in `.env`. `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and `MINIMAX_API_KEY` are required.

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | *(required)* | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | *(required)* | Your numeric Telegram user/chat ID |
| `MINIMAX_API_KEY` | *(required)* | API key from platform.minimax.io |
| `TELEGRAM_BOT_TOKEN_2` | *(optional)* | Second bot token for a second recipient |
| `TELEGRAM_CHAT_ID_2` | *(optional)* | Second chat ID for a second recipient |
| `HEADLESS` | `true` | `false` shows the browser window (local dev only) |
| `CHECK_INTERVAL` | `120` | Seconds between each poll of the tennis page |
| `SIMILARITY_THRESHOLD` | `0.75` | Score above which a pair is flagged as duplicate |
| `MIN_SIDE_SCORE` | `0.60` | Minimum score each side (home/away) must reach |
| `AI_ANALYSIS` | `true` | Set to `false` to disable the MiniMax AI layer |

---

## How the duplicate detection works

### Rule-based layer (runs every poll)

For every pair of live matches, the model scores 0.0–1.0:

| Condition | Score |
|-----------|-------|
| Identical name strings | 1.00 |
| Same surname + same first initial (one may be abbreviated) | 0.92 |
| Same surname + similar full first names (transliteration) | 0.85 |
| Same surname + no initial info to compare | ~0.70 |
| Same surname but **different** first initial | 0.15 (not a duplicate) |
| Surname mismatch — fuzzy whole-string fallback | ≤ 0.70 |

A pair is flagged when the **average** of both sides ≥ `SIMILARITY_THRESHOLD` **and** neither side scores below `MIN_SIDE_SCORE`.

### AI layer (MiniMax-M2.7)

After the rule-based pass, the full list of live matches is sent to **MiniMax-M2.7** which independently looks for:
- **DUPLICATE** — same match listed twice (including reversed side order and name format differences)
- **PLAYER_CONFLICT** — same real player appearing in two different live matches simultaneously

### False positive filter

Before any alert is sent, the monitor opens both match pages in separate browser tabs and checks their status. If one match is `live` and the other is `not started`, the pair is treated as a false positive (different scheduled dates) and the alert is suppressed. A report is still saved to `anomaly_reports/`.

---

## Telegram alert examples

**Duplicate detected:**
```
🎾 Possible duplicate tennis match! (MiniMax-M2.7)
Confidence: High

Match A: Butvilas, Edas  vs  Imamura, Masamichi
Match B: Butvilas, E     vs  Imamura, M

AI analysis: Both entries refer to the same match with abbreviated vs full first names.
```

**Daily heartbeat (07:00 UTC):**
```
💓 Monitor heartbeat
Uptime: 6h 0m  |  2026-03-20 07:00 UTC

🎾 Live matches (3):
  1. Butvilas, Edas vs Imamura, Masamichi
  2. Shimizu Y vs Romios M C
  3. Riera, Julia/Romero Gormaz, Leyre vs Alcala Gurri, M/Mintegi del Olmo, A

📋 No anomalies since last heartbeat.
```
