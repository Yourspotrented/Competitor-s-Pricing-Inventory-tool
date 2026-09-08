# Deploying

Two pieces, two hosts:

| Piece | Host | What it is |
|---|---|---|
| Backend — API, hourly schedulers, and the dashboard served from `/` | **Render** (web service) | `backend/`, see `render.yaml` |
| Dashboard — the same `index.html`, published as a static page | **Vercel** | see `vercel.json` / `vercel-build.sh` |

The dashboard on Vercel calls the Render API cross-origin. It works because
the backend already allows any origin, and the page carries the password as a
header rather than a cookie.

You don't strictly need Vercel — the Render URL serves the dashboard itself.
Vercel gives the team a nicer URL and keeps the page up even while Render is
redeploying.

## 0. Push the repo

The project is a git repo (`main` branch, first commit made). Create a
**private** GitHub repo and push:

```bash
cd competitors_data_finder
git remote add origin git@github.com:<you>/competitors-data-finder.git
git push -u origin main
```

`.env`, the database and `.venv` are gitignored — secrets go into the hosts'
environment settings, never into git.

## 1. Render — backend

1. Dashboard → **New → Blueprint** → pick the repo. Render reads `render.yaml`.
2. It will prompt for the `sync: false` values. Set at least:
   - `DASHBOARD_PASSWORD` — what the team types to open the dashboard
   - `TEAMS_WEBHOOK_URL`, `EVENTS_TEAMS_WEBHOOK_URL`, `LYSTED_TEAMS_WEBHOOK_URL`
     (copy from your local `backend/.env`; blank = that flow's alerts are recorded but not sent)
   - Notion / SharePoint keys if you want those features
3. Deploy. First build takes a couple of minutes; then open
   `https://<service>.onrender.com/health` → `{"status":"ok"}`, and `/` → the
   browser asks for the password → dashboard.

**Plan:** `render.yaml` asks for **Starter**. Do not downgrade to Free — free
instances sleep after 15 idle minutes and the schedulers stop with them, so
nothing gets scanned and nothing gets alerted.

**Storage:** the blueprint mounts a 1 GB persistent disk at `/var/data` and
points `DATABASE_PATH` there, so the SQLite database survives deploys.

### Carry your local database across (recommended)

A fresh database means the first run re-geocodes every facility (~10 min of
Nominatim/SpotHero calls) and, worse, has no alert baseline — every currently
low or sold-out lot looks like a brand-new crossing and lands in Teams at once.
Either copy the local database up before enabling the webhooks:

```bash
# from the service's Connect → SSH panel you get an address like
#   srv-xxxx@ssh.oregon.render.com
scp backend/scarcity.db srv-xxxx@ssh.oregon.render.com:/var/data/scarcity.db
```

then restart the service — or deploy with the webhook variables blank, let the
first scheduled runs seed the baseline, and fill the webhooks in afterwards.

### Stop the local copy

Once Render is running, stop uvicorn on your machine. Two instances with the
same webhooks post every alert twice.

## 2. Vercel — dashboard

1. **Add New → Project** → import the same repo. Framework preset: **Other**.
   `vercel.json` supplies the build command and output directory.
2. **Environment Variables** → add `API_BASE` = your Render URL, no trailing slash:
   `https://competitors-data-finder.onrender.com`
3. Deploy. The build copies `backend/static/index.html` to `out/` and writes
   `out/config.js` with that URL.
4. Open the Vercel URL. The page loads, its first API call gets a 401 from
   Render, and the page prompts for the password once (remembered in the
   browser).

Change `API_BASE` → redeploy; the page is rebuilt with the new value.

## 3. Password

`DASHBOARD_PASSWORD` on Render is the only gate. Leave it unset locally and
the app is open, as before. Set it and:

- opening the Render URL directly → the browser's own username/password prompt
  (any username, that password)
- the Vercel page → its own prompt on first API call, then an
  `X-Dashboard-Key` header on every request
- `/health` stays open for Render's health checks

Rotate it by changing the variable and redeploying; everyone re-enters it.

## Local development — unchanged

```bash
cd backend && .venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
```

No `DASHBOARD_PASSWORD`, no `API_BASE`; `/config.js` is served by the app
with an empty base and everything stays same-origin.

## Checklist before handing out the link

- [ ] `DASHBOARD_PASSWORD` set on Render
- [ ] `/health` returns ok; `/` prompts for the password
- [ ] database copied up, or webhooks left blank for the first runs
- [ ] all three Teams webhooks set and a test card received
- [ ] local uvicorn stopped
- [ ] Vercel `API_BASE` points at Render and the page loads data
