# Free Deploy Setup

This setup runs the static dashboard on Vercel and the intraday Python backend
on a Render Free web service.

## 1. Deploy Backend On Render

Create a new Render Blueprint from this repository. Render will read
`render.yaml` and create a Free Python web service.

The service uses:

```bash
pip install -r requirements-options.txt
python scripts/live_server.py --host 0.0.0.0 --port $PORT --no-github-pull
```

After deploy, copy the Render service URL. It should look like:

```text
https://quant-research-live.onrender.com
```

Render Free will spin down after 15 minutes without inbound traffic. Keeping the
dashboard open during market hours lets the frontend polling keep the backend
active while you are watching it.

## 2. Deploy Frontend On Vercel

Import this repository into Vercel as a static project. The root `vercel.json`
routes:

- `/` to `frontend/index.html`
- `/assets/*` to `frontend/assets/*`

No Node build step is required.

## 3. Connect Frontend To Backend

Open the Vercel URL once with your Render backend URL in the `apiBase` query
parameter:

```text
https://your-vercel-app.vercel.app/?apiBase=https://quant-research-live.onrender.com
```

The dashboard saves that backend URL in browser local storage, so later visits
to the Vercel URL can call Render without the query parameter.

To switch to a different backend, open the Vercel URL again with a new `apiBase`
value.

## 4. EOD Data

Keep using GitHub Actions for EOD data collection after market close. The
intraday backend is for market-open monitoring; the EOD workflow is the better
free path for scheduled end-of-day snapshots.
