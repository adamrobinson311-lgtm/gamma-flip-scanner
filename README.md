# 📊 S&P 500 Gamma Flip Scanner

Auto-updating dashboard that scans all S&P 500 stocks for gamma flip levels daily.
Hosted on Cloudflare Pages, password-protected via Cloudflare Access, updated every
weekday at 4:30 PM ET via GitHub Actions.

---

## 🗂️ File Structure

```
├── gamma_flip_dashboard.html   ← web dashboard (the site)
├── gamma_scanner.py            ← Python scanner script
├── gamma_results.json          ← auto-generated daily (committed by Actions)
├── requirements.txt
└── .github/
    └── workflows/
        └── daily_scan.yml      ← GitHub Actions schedule
```

---

## 🚀 Step-by-Step Setup

### Step 1 — Create your GitHub repository

1. Go to https://github.com/new
2. Create a **private** repository (e.g. `gamma-flip-scanner`)
3. Clone it locally:
   ```bash
   git clone https://github.com/YOUR_USERNAME/gamma-flip-scanner.git
   cd gamma-flip-scanner
   ```
4. Copy all project files into the folder, then push:
   ```bash
   git add .
   git commit -m "Initial commit"
   git push origin main
   ```

---

### Step 2 — Connect to Cloudflare Pages

1. Go to https://dash.cloudflare.com → **Workers & Pages** → **Create application** → **Pages**
2. Click **Connect to Git** → authorize GitHub → select your `gamma-flip-scanner` repo
3. Configure build settings:
   - **Framework preset:** None
   - **Build command:** *(leave blank)*
   - **Build output directory:** `/` *(root)*
4. Click **Save and Deploy**

Cloudflare will deploy instantly. Your site URL will be something like:
`https://gamma-flip-scanner.pages.dev`

Every time GitHub Actions commits new `gamma_results.json`, Cloudflare Pages
auto-detects the push and re-deploys within ~30 seconds.

---

### Step 3 — Add password protection (Cloudflare Access)

This is free for up to 50 users.

1. In Cloudflare dashboard → **Zero Trust** (left sidebar)
2. **Access** → **Applications** → **Add an application**
3. Choose **Self-hosted**
4. Fill in:
   - **Application name:** Gamma Scanner
   - **Application domain:** `gamma-flip-scanner.pages.dev` (your Pages URL)
5. Click **Next** → under **Policy**, add a rule:
   - **Rule name:** Password Gate
   - **Action:** Allow
   - **Include:** Selector = **One-time PIN** + your email address
     *(Cloudflare will email you a one-time code each login — no account needed for visitors)*

   **OR for a static shared password:**
   - Use selector **Email** → list allowed email addresses
   - Anyone not on the list is blocked

6. Click **Save**

> 💡 **Simpler alternative:** In Pages → your site → **Settings** → **Access Policy**
> → enable **Cloudflare Access** directly. Same result, fewer clicks.

---

### Step 4 — Verify GitHub Actions can push back to the repo

The workflow commits `gamma_results.json` back to the repo after each scan.
By default, GitHub Actions has write permission — but confirm it:

1. In your GitHub repo → **Settings** → **Actions** → **General**
2. Scroll to **Workflow permissions**
3. Select **Read and write permissions** → Save

---

### Step 5 — Run your first scan manually

Don't wait until 4:30 PM — trigger it now:

1. In your GitHub repo → **Actions** tab
2. Click **Daily Gamma Flip Scan** → **Run workflow** → **Run workflow**
3. Watch the logs (~20–30 min for full S&P 500)
4. When done, `gamma_results.json` appears in your repo
5. Cloudflare Pages auto-redeploys → visit your URL → real data loads 🎉

---

### Step 6 — Customize the scan schedule

Edit `.github/workflows/daily_scan.yml`:

```yaml
# Current: 4:30 PM ET on weekdays
- cron: '30 21 * * 1-5'

# Examples:
# 6:00 PM ET (after extended hours settle)
- cron: '0 23 * * 1-5'

# 9:00 AM ET (pre-market)
- cron: '0 14 * * 1-5'
```

Cron uses UTC. ET = UTC-5 (winter) / UTC-4 (summer). Adjust accordingly.

---

## ⚙️ Scanner Options

```bash
# Full S&P 500 (default)
python gamma_scanner.py

# Limit to first 20 tickers (for testing)
python gamma_scanner.py --limit 20

# Specific tickers only
python gamma_scanner.py --tickers AAPL MSFT NVDA TSLA SPY QQQ

# Custom output path
python gamma_scanner.py --output my_results.json

# More/fewer parallel workers (reduce if hitting rate limits)
python gamma_scanner.py --workers 4
```

---

## 🔧 Troubleshooting

| Problem | Fix |
|---|---|
| Actions workflow fails with 403 | yfinance rate-limited — reduce `--workers` to 4 in the YAML |
| Dashboard shows demo data on live site | First scan hasn't run yet; trigger manually (Step 5) |
| Cloudflare Access blocks everything | Check your email is in the Access policy allow list |
| `gamma_results.json` not updating | Check Actions → Workflow permissions is set to Read+Write |
| Scan takes >45 min and times out | Reduce scope: add `--limit 200` to the scanner command in the YAML |

---

## 📐 How Gamma Flip is Calculated

1. Fetch options chains (calls + puts) for expirations within 60 days via `yfinance`
2. Estimate per-strike gamma using Black-Scholes (yfinance doesn't provide Greeks directly)
3. Compute Net Gamma Exposure (GEX):
   ```
   GEX = (call_gamma × call_OI − put_gamma × put_OI) × spot² × 0.01 × 100
   ```
4. Accumulate GEX from highest to lowest strike
5. Interpolate the strike where cumulative GEX crosses zero → **Gamma Flip Level**

When price is **above** the flip: dealers are net long gamma (they sell rallies, buy dips → stabilizing)
When price is **below** the flip: dealers are net short gamma (they chase moves → amplifying)
