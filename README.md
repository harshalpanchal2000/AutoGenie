# AutoGenie: free, self-hosted Google Maps lead generation (n8n + Telegram)

Send `/leads plumbers in Chicago, IL` to your Telegram bot, or let a schedule run it. A headless
browser on **your own VPS** collects businesses from Google Maps, cleans and de-duplicates them, and
sends them back to Telegram as formatted lead cards plus a CSV file.

**No paid APIs.** No Google Places billing, no Apify, no SerpAPI. Everything is open source:
n8n, Playwright/Chromium, FastAPI, SQLite and Caddy.

```
                                  ┌──────────────────────── your VPS (Docker Compose) ───────────────────────┐
Telegram  /leads plumbers in X ──►│ Caddy (free HTTPS) ──► n8n                                                │
                                  │                         │ Telegram Trigger / Schedule Trigger             │
                                  │                         │ parse + validate command (allow-listed chats)   │
                                  │                         │ POST http://scraper:8000/scrape  (internal)     │
                                  │                         ▼                                                 │
                                  │                      scraper (Python + Playwright headless Chromium)      │
                                  │                         • scroll Maps results, open each place            │
                                  │                         • name, category, phone, website, address,        │
                                  │                           rating, reviews, coordinates, Maps link         │
                                  │                         • optional: find emails on the business website   │
                                  │                         • clean, normalise, filter, dedupe                │
                                  │                         • SQLite "seen" DB → never re-sends a lead        │
                                  │                         ▼                                                 │
Telegram  ◄── summary + lead cards + CSV ──── n8n: final dedupe, format, split into ≤4096-char messages     │
                                  └───────────────────────────────────────────────────────────────────────────┘
```

> **Be responsible.** Automated access to Google Maps may conflict with Google's Terms of Service.
> Keep the volume low (the defaults are deliberately gentle: one job at a time, 3 tabs, random
> delays), use it for your own prospecting, and follow the anti-spam and privacy laws that apply
> to your outreach (CAN-SPAM, GDPR, TCPA…).

---

## Repository layout

```
.
├── docker-compose.yml          # caddy + n8n + scraper
├── Caddyfile                   # HTTPS reverse proxy for n8n (auto Let's Encrypt)
├── .env.example                # all configuration lives here → copy to .env
├── n8n/
│   └── lead-gen-telegram.json  # importable n8n workflow
├── scraper/
│   ├── Dockerfile              # based on the official Playwright image (Chromium included)
│   ├── requirements.txt
│   └── app/
│       ├── main.py             # FastAPI: POST /scrape, GET /stats, DELETE /seen, GET /health
│       ├── maps.py             # Playwright Google Maps scraper (selectors in one place)
│       ├── cleaning.py         # normalisation, dedup keys, filters
│       ├── enrich.py           # optional email finder (homepage + /contact)
│       ├── storage.py          # SQLite store of already-delivered leads
│       └── cli.py              # run a scrape from the terminal for testing
└── scripts/
    └── install-docker.sh       # one-time VPS bootstrap (Docker, swap, firewall)
```

---

## What you need

| Item | Notes |
|---|---|
| VPS | Ubuntu 22.04/24.04 (Debian works too). **2 GB RAM recommended**; 1 GB works with the swap file the install script creates. ~5 GB disk. |
| Domain name | Telegram only sends bot updates to **HTTPS** webhooks. A free [DuckDNS](https://www.duckdns.org) subdomain works fine. (No domain? See [Schedule-only mode](#no-domain-schedule-only-mode).) |
| Telegram account | To create the bot with @BotFather. |

Node.js and Python are **not** installed on the host. They run inside the containers. Docker is the
only dependency.

---

## Step-by-step setup

### 1. Create the Telegram bot

1. In Telegram, open **@BotFather** → `/newbot` → choose a name and username → copy the **bot token**
   (`123456:ABC-...`).
2. Get your numeric **chat ID**: message **@userinfobot** (it replies with your ID).
3. *(Optional) Group:* add the bot to the group, send any message, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser. The group ID is the negative number
   under `chat.id`. Do this **before** step 7, because after the webhook is set, `getUpdates` stops working.
   In a group, also send `/setprivacy` → *Disable* to @BotFather so the bot can see commands, or
   address commands as `/leads@YourBot …`.
4. *(Optional) Channel for scheduled leads:* add the bot to the channel as an **admin** with
   "Post messages". Use `@channelname` or the `-100…` ID as `SCHEDULE_CHAT_ID`.

### 2. Point a domain at the VPS

Create a DNS **A record** such as `leads.yourdomain.com → <VPS IP>`.
With DuckDNS, create `yourname.duckdns.org` and set its IP to your VPS.
Check with `ping leads.yourdomain.com`. It must resolve to the VPS **before** you start Caddy.

### 3. Get the code onto the VPS and install Docker

```bash
ssh you@your-vps
git clone https://github.com/harshalpanchal2000/AutoGenie.git
cd AutoGenie
sudo bash scripts/install-docker.sh     # Docker + compose plugin, 2G swap, ufw (22/80/443)
# log out and back in so your user can run `docker` without sudo
```

### 4. Configure `.env`

```bash
cp .env.example .env
openssl rand -hex 32   # run twice: one for N8N_ENCRYPTION_KEY, one for SCRAPER_API_KEY
nano .env
```

Minimum to change:

```dotenv
N8N_DOMAIN=leads.yourdomain.com
N8N_ENCRYPTION_KEY=<random>
SCRAPER_API_KEY=<random>
TZ=America/Chicago
TELEGRAM_ALLOWED_CHAT_IDS=123456789          # you (and optionally group IDs), comma-separated
SCHEDULE_CHAT_ID=123456789                   # or @yourchannel / -100xxxxxxxxxx
SCHEDULED_JOBS=plumbers | Chicago, IL | 20 ; roofers | Austin, TX | 15 | --phone --emails
```

> Keep `N8N_ENCRYPTION_KEY` safe. n8n encrypts the stored credentials (your bot token) with it.

### 5. Start the stack

```bash
docker compose up -d --build        # first build downloads the ~1.5 GB Playwright image
docker compose ps                   # all three should be "running" / "healthy"
docker compose logs -f caddy        # wait for "certificate obtained successfully", then Ctrl+C
```

### 6. Smoke-test the scraper (no n8n involved)

```bash
docker compose run --rm scraper python -m app.cli "plumbers" "Chicago, IL" --limit 3
```

You should see JSON with 3 leads (name, phone, website, address, rating…). The CLI does **not**
mark leads as seen, so testing doesn't hide leads from the real pipeline.
If this returns 0 leads, see [Troubleshooting](#troubleshooting) before continuing.

### 7. Set up n8n

1. Open `https://leads.yourdomain.com` and create the owner account.
2. **Credentials → Create credential → Telegram API**. Paste the bot token and name it `Telegram Bot`.
3. **Workflows → Create → ⋯ menu → Import from File…** and choose `n8n/lead-gen-telegram.json`.
   Copy it to your computer first (`scp you@vps:AutoGenie/n8n/lead-gen-telegram.json .`), or use
   **Import from URL** with the file's *raw* GitHub URL if your repo is public.
4. Open each Telegram node (**Telegram Trigger, Send 'searching' message, Reply with help / error,
   Send leads, Send CSV, Report scrape error**) and select the `Telegram Bot` credential.
5. *(Optional)* Open **Schedule Trigger** and change the cron. The default is `0 9 * * 1-5`
   (09:00 on weekdays, in `TZ`). To turn scheduling off, disable that node.
6. **Save**, then switch the workflow **Active** (called **Publish** in n8n 2.x). Activation
   registers the Telegram webhook automatically.

### 8. Use it

Send your bot:

```
/help
/leads plumbers in Chicago, IL
/leads dentists | Austin, TX --limit 30 --no-website
/leads roofers near Denver --phone --min-rating 4 --emails
/leads hvac in Phoenix --all
```

| Flag | Meaning |
|---|---|
| `--limit N` / `-n N` | Number of **new** leads to return (default `DEFAULT_LEAD_LIMIT`, max 100) |
| `--phone` | Only businesses with a phone number |
| `--has-website` | Only businesses with a website |
| `--no-website` | Only businesses **without** a website (good prospects for web-design agencies) |
| `--min-rating 4.2` / `--max-rating 4` | Rating filters (`--max-rating` finds businesses that may want reputation help) |
| `--min-reviews 10` | Minimum number of reviews |
| `--emails` | Visit each business website (homepage, `/contact`, `/about`) and extract public emails. Slower. |
| `--all` | Include leads that were already sent before |
| `--closed` | Include permanently closed places (excluded by default) |

Syntax is `niche in location`, `niche near location` or `niche | location`.

You get:
1. A "searching…" acknowledgement
2. A summary (new leads, duplicates skipped, filtered out, duration)
3. Lead cards (name, rating, category, tap-to-copy phone, website, emails, address, Maps link)
4. A **CSV file** with all leads, ready for a spreadsheet or CRM

A run usually takes 1–5 minutes, depending on the limit and `--emails`.

---

## How cleaning and de-duplication work

- **Cleaning** (`scraper/app/cleaning.py`): strips Google's icon glyphs and extra whitespace,
  normalises phones (display format plus digits), unwraps Google redirect links, removes `utm_*` and
  other tracking parameters, lower-cases hosts, extracts the bare domain, and parses ratings, review
  counts and coordinates.
- **Within one run**, a place is a duplicate if it has the same Google place ID, the same phone
  number, or the same website domain (except shared hosts such as facebook.com, yelp.com or wixsite.com).
- **Across runs**, every delivered lead goes into SQLite (`/data/leads.db` in the `scraper_data`
  volume). Places already delivered are skipped **before** their page is opened, so repeating the
  same search is fast and returns only fresh leads. `--all` bypasses this.
- **In n8n**, the *Clean, dedupe & format* node runs a final safety pass (place ID, phone,
  name+address) before formatting.

Maintenance:

```bash
# how many leads have been delivered
docker compose exec scraper python -c "from app.storage import LeadStore; print(LeadStore('/data/leads.db').count())"

# forget everything (all leads become "new" again)
docker compose exec scraper python -c "from app.storage import LeadStore; print(LeadStore('/data/leads.db').clear())"

# forget one search only (exact text as shown in the summary, e.g. "plumbers in Chicago, IL")
docker compose exec scraper python -c "from app.storage import LeadStore; print(LeadStore('/data/leads.db').clear('plumbers in Chicago, IL'))"

# copy the database to the host
docker compose cp scraper:/data/leads.db ./leads.db
```

---

## Scheduled runs

`SCHEDULED_JOBS` in `.env` holds `;`-separated jobs, each `niche | location | limit | flags`:

```dotenv
SCHEDULED_JOBS=plumbers | Chicago, IL | 20 ; electricians | Chicago, IL | 20 | --no-website ; roofers | Austin, TX | 15 | --phone --emails
```

Jobs run one after another and are delivered to `SCHEDULE_CHAT_ID`. Because of the seen-leads
database, each daily run only posts businesses you haven't received yet.
After editing `.env`, run `docker compose up -d` to apply it.

---

## Scraper HTTP API (internal)

n8n calls the scraper at `http://scraper:8000` with header `X-API-Key: $SCRAPER_API_KEY`.
The scraper port is not published to the internet.

`POST /scrape`
```json
{
  "query": "plumbers",
  "location": "Chicago, IL",
  "limit": 20,
  "filters": { "require_phone": false, "require_website": false, "without_website": false,
               "min_rating": null, "max_rating": null, "min_reviews": null, "include_closed": false },
  "exclude_seen": true,
  "mark_seen": true,
  "enrich_emails": false,
  "meta": { "chat_id": "123" }
}
```
The response contains `leads[]`, `stats`, `count`, the echoed `meta`, `csv_filename` and `csv_base64`.
Other endpoints are `GET /health`, `GET /stats` and `DELETE /seen?query=...`.
A CAPTCHA from Google returns HTTP **429** with an explanation, which n8n forwards to Telegram.

---

## Troubleshooting

**The CLI returns 0 leads or empty fields.** Google changes its Maps markup from time to time. All
selectors are at the top of `scraper/app/maps.py` (`SELECTORS`, `EXTRACT_JS`). To inspect the
current markup, open a place in Chrome, right-click → *Inspect*, and look for the elements holding the
name (`h1`), the address button (`data-item-id="address"`), the phone (`data-item-id^="phone:tel:"`)
and the website (`data-item-id="authority"`). Update the selectors, then run
`docker compose up -d --build scraper`.

**"Google returned a CAPTCHA" / HTTP 429.** You are scraping too fast or too much from one IP. Wait an
hour or more, reduce `SCRAPER_CONCURRENCY` to 1–2, and use smaller limits. Optionally set
`SCRAPER_PROXY` (for example a SOCKS tunnel to another server you own).

**The EU consent page blocks results.** The scraper clicks "Reject all"/"Accept all" automatically.
If your VPS region shows a different consent layout, add its button selector to
`SELECTORS["consent_buttons"]`.

**The bot does not react to `/leads`.**
- Is the workflow **Active/Published**?
- Is your chat ID in `TELEGRAM_ALLOWED_CHAT_IDS`? Unknown chats are ignored silently.
- `https://api.telegram.org/bot<TOKEN>/getWebhookInfo` should show `url: https://<N8N_DOMAIN>/webhook/...`
  with no `last_error_message`.
- `docker compose logs caddy`: the certificate must have been issued (DNS correct, ports 80/443 open).
- One bot token can only have one webhook. Don't use the same bot anywhere else.
- In groups, disable privacy mode in @BotFather or use `/leads@YourBot ...`.

**"access to env vars denied" in n8n.** The workflow reads configuration from `$env`. Check that
`N8N_BLOCK_ENV_ACCESS_IN_NODE=false` is present in `docker-compose.yml`, and recreate the container:
`docker compose up -d`.

**Telegram "chat not found".** The bot must have been started by that user (press *Start*) or be a
member (or an admin, for channels) of that group or channel.

**Out of memory / Chromium crashes.** Use at least 2 GB RAM plus swap, keep `SCRAPER_CONCURRENCY` at
2–3, and don't remove `shm_size: 1gb` from `docker-compose.yml`.

**Logs**
```bash
docker compose logs -f scraper     # every scrape logs its query and stats
docker compose logs -f n8n
```

---

## No domain? (schedule-only mode)

Without HTTPS, Telegram cannot deliver *commands* to n8n, but n8n can still *send* leads on a
schedule:

1. Remove the `caddy` service from `docker-compose.yml`. In the `n8n` service, set
   `N8N_PROTOCOL=http`, `N8N_SECURE_COOKIE=false` and `WEBHOOK_URL=http://localhost:5678/`.
2. Reach n8n through an SSH tunnel: `ssh -L 5678:localhost:5678 you@vps`, then open
   `http://localhost:5678`.
3. Import the workflow, **disable the Telegram Trigger node**, and activate it. The schedule branch
   sends leads to `SCHEDULE_CHAT_ID`.

---

## Updating

```bash
git pull
docker compose pull n8n caddy
docker compose up -d --build
```

Pin `N8N_VERSION` in `.env` (for example `2.4.0`) if you want upgrades only when you choose.
If you bump the Playwright version in `scraper/requirements.txt`, change the image tag in
`scraper/Dockerfile` to the same version.
