# Setup — get a live daily feed running in ~10 minutes

You end up with two things, both free, both automatic after setup:
1. A URL like `https://<you>.github.io/dealflow/` showing every thesis-matching
   company found so far, refreshed daily.
2. (Optional) A daily Slack message listing only what's *new* since yesterday.

## 1. Create the repo
1. Go to github.com → New repository → name it `dealflow` (private is fine) → Create.
2. On your machine, unzip this folder and push it:
```bash
cd dealflow-engine
git init
git add .
git commit -m "Initial dealflow engine"
git branch -M main
git remote add origin https://github.com/<your-username>/dealflow.git
git push -u origin main
```

## 2. Turn on GitHub Pages (this gives you the feed URL)
1. In the repo: Settings → Pages.
2. Under "Build and deployment", set Source to "Deploy from a branch".
3. Branch: `main`, folder: `/docs`. Save.
4. GitHub shows you the URL (`https://<you>.github.io/dealflow/`) — it goes
   live after the first successful workflow run (next step).

## 3. Turn on the daily job
1. Repo → Actions tab → you'll see "Dealflow feed" listed → click it →
   "Run workflow" (this does today's run immediately so you don't wait for
   tomorrow's cron).
2. After it finishes (~1-2 min), check `docs/index.html` was updated and
   your Pages URL shows the feed.
3. From here it runs automatically every day at 13:00 UTC — nothing else to do.

## 4. (Optional) Add nuanced LLM scoring
Without this, matches are keyword-only — fast and free, but literal.
1. Get an API key from console.anthropic.com.
2. Repo → Settings → Secrets and variables → Actions → New repository secret.
3. Name: `ANTHROPIC_API_KEY`, value: your key. Save.
Every run now also asks Claude to score the top 25 matches 1-5 with a
one-line rationale per company — shown on the feed page automatically.

## 5. (Optional) Add a Slack digest of what's new each day
1. In Slack: create an Incoming Webhook for the channel you want
   (api.slack.com/apps → your app → Incoming Webhooks → Add New Webhook).
2. Copy the webhook URL.
3. Repo → Settings → Secrets and variables → Actions → New repository secret.
4. Name: `SLACK_WEBHOOK`, value: the URL. Save.
Next run posts a message listing only companies new since the last commit —
distinct from the full running feed on the Pages site.

## Keeping it current as YC batches change
`dealflow.yml` currently tracks `summer-2026,fall-2026`. When a new batch
opens, edit the `--batch` value in `.github/workflows/dealflow.yml` (two
places) to add the new slug, e.g. `summer-2026,fall-2026,winter-2027`. Old
batches don't need to be removed — YC's batches close, so they stop growing
and just sit in your feed as historical record.

## Adjusting your theses
Edit `theses.json` directly in GitHub's web UI (or locally + push) — no
code changes needed, and no need to re-run anything manually; it takes
effect on the next scheduled run. Keep an eye on `matched_keywords` in
`dealflow_latest.csv` after a few days and tighten any keyword that's firing
on too many irrelevant companies.

## Troubleshooting
- **Workflow fails on push**: make sure the repo has Action permissions
  set to "Read and write" — Settings → Actions → General → Workflow
  permissions.
- **Feed page 404s**: Pages can take a few minutes after the first deploy;
  also confirm the source folder is set to `/docs`, not `/ (root)`.
- **No Slack message but no error either**: the workflow only posts when
  there's something new — check `dealflow_latest.csv` in the repo to
  confirm scoring is actually running.
