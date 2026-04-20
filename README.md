# EdgeOS — MLB · NBA · NFL Betting Models

Research-grade sports betting model dashboard. Not validated. Not financial advice.

## What it is

A single-file HTML dashboard running three model families:

- **MLB**: Totals (seeded Monte Carlo, 10k sims), Moneyline (logit win prob), Run Line (margin simulation with game correlation and DQ shrinkage)
- **NBA**: Totals, Moneyline, and Spread via OffRtg/DefRtg/Pace projection
- **NFL**: Same structure, EPA/play inputs — active in-season (September 2026)

All models output edge vs market implied, EV at posted odds, and a data quality score (DQ) that gates action bets.

## Auto-update

A GitHub Actions workflow runs every morning at 9 AM CT:

1. Pulls today's MLB schedule and probable pitchers from the MLB Stats API (free)
2. Fetches xERA from Baseball Savant (free)
3. Fetches totals odds from The Odds API (requires API key)
4. Rebuilds `edgeos_combined.html` with today's slate
5. Commits and pushes — GitHub Pages serves the updated file

## Setup

### 1. Fork or clone this repo

### 2. Enable GitHub Pages

Go to **Settings → Pages** and set the source to the `main` branch, root folder.

Your site will be live at `https://yourusername.github.io/edgeos`

### 3. Add your Odds API key (optional)

Without it, the model still runs but uses a placeholder total of 8.5 for all games. With it, you get real posted totals.

Get a free key at [the-odds-api.com](https://the-odds-api.com) (500 requests/month on the free tier).

Go to **Settings → Secrets and variables → Actions → New repository secret**:

```
Name:  ODDS_API_KEY
Value: your_key_here
```

### 4. Trigger a manual run (optional)

Go to **Actions → Daily Slate Update → Run workflow** to generate today's slate immediately without waiting for the 9 AM schedule.

### 5. Custom domain (optional)

Buy a domain (~$12/year), add it in **Settings → Pages → Custom domain**, and add a `CNAME` file to the repo root with your domain name.

## File structure

```
edgeos_combined.html     # The full dashboard — auto-updated daily
index.html               # Landing page
scripts/
  edgeos_update.py       # The slate updater — runs in GitHub Actions
.github/
  workflows/
    update.yml           # The daily cron job
```

## Running the updater locally

```bash
pip install requests python-dotenv

# Create a .env file with your key
echo "ODDS_API_KEY=your_key_here" > .env

# Run against today's slate
python scripts/edgeos_update.py --template edgeos_combined.html --output edgeos_combined.html

# Run against a specific date
python scripts/edgeos_update.py --template edgeos_combined.html --output edgeos_combined.html --date 2026-04-19
```

## Status

- ✅ MLB models complete (Totals v10, ML v1, RL v2)
- ✅ NBA models complete (playoffs slate live)
- ✅ NFL model infrastructure complete (empty slate — active September 2026)
- ⚠️ Zero graded bets — model is unvalidated
- 🎯 Target: 300-500 graded bets before any monetization consideration

## Disclaimer

This is a research tool in early development. All models use weights that are educated estimates, not fitted to historical data. No picks have been verified against real outcomes. Do not use this to make betting decisions. Track results, calibrate, and wait for statistical significance.
