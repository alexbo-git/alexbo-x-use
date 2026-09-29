# Railway deployment

This fork is configured as an autonomous X engagement worker for Kalyvox.

## 1. Create the Railway service
Deploy this GitHub repository as a normal service. Railway will use the root `Dockerfile`.

Do **not** create a public domain: this is a background worker.

## 2. Add one persistent volume
Mount a Railway Volume at:

```
/app/data
```

It stores cookies generated from environment variables, processed-tweet history, worker state, and x-use metrics.

## 3. Required variables

```
X_AUTH_TOKEN=...
X_CT0=...
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-5-nano
```

`OPENAI_BASE_URL` is optional. Leave it empty for OpenAI; set it for another OpenAI-compatible provider.

The two X cookie values are session credentials. Never commit them.

## 4. Optional worker variables

```
WORKER_TIMEZONE=Europe/Paris
WORKER_START_HOUR=8
WORKER_END_HOUR=22
WORKER_MIN_INTERVAL_MINUTES=20
WORKER_MAX_INTERVAL_MINUTES=40
WORKER_REPLY_DAILY_TARGET=20
WORKER_LIKE_DAILY_TARGET=30
WORKER_KEYWORDS_PER_CYCLE=2
WORKER_ERROR_PAUSE_HOURS=2
```

Optional:
- `X_TARGET_KEYWORDS`: semicolon-separated keyword override
- `X_PERSONA`: full persona override
- `X_PROXY`: stable proxy URL if one is used

## 5. What the worker does

- Original posts: **never**. Buffer remains responsible for publishing.
- Reposts/content curation/community posting: disabled.
- Autonomous keyword replies: enabled.
- Autonomous likes: enabled.
- Two keywords are rotated per cycle.
- Maximum one successful reply per keyword per cycle.
- Daily targets are measured from actual x-use metrics, not assumed attempts.
- On repeated errors the worker pauses automatically before retrying.

Default audience mix covers SaaS/AI builders, SMB/growth/customer-service topics, voice AI, and selected small-business verticals.

## 6. First launch

After Railway deploys, inspect the deployment logs. You should see:

```
Kalyvox X worker started...
Cycle keywords: ...
Starting pipeline=keyword_replies...
```

If authentication fails, replace `X_AUTH_TOKEN` and `X_CT0` with fresh values from an authenticated x.com browser session and redeploy/restart.

## 7. Operating it

Normal operation requires no daily action. Use Railway to:
- inspect logs
- restart/pause the service
- change daily targets/keywords/persona through variables
- replace expired X cookies

State survives deployments through the `/app/data` volume.

## Web admin

Set `ADMIN_PASSWORD` in Railway Variables, then enable Railway Public Networking / Generate Domain.

Open the generated URL. The browser will ask for HTTP Basic credentials:
- username: anything (for example `admin`)
- password: the exact value of `ADMIN_PASSWORD`

Available controls:
- dashboard: `/`
- machine-readable status: `/status`
- health check without auth: `/health`
- buttons: Run now, Pause, Resume

The admin never exposes X cookies or the OpenAI key.

## Growth measurement

The worker records one profile snapshot per day in `/app/data/growth/` and exposes it at `/growth` and in the dashboard.

It tracks current followers/following, 1-day and 7-day follower deltas, and 7-day followers gained per 10 automated replies. Set `X_HANDLE` (without @) if automatic handle detection ever fails.
