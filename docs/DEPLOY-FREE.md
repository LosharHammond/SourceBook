# Running Sourcebook online for free: Vercel + Supabase + GitHub Actions

| Part | Where | Free plan limit that matters |
|---|---|---|
| Website, sign-in, searches | Vercel Hobby | a request may run up to 300 s, so a search checks for 4 minutes and then hands over |
| Data | Supabase, a second free project just for Sourcebook | 500 MB; paused after a week with no activity (the crawler keeps it busy) |
| Hourly crawler | GitHub Actions | free in a public repository; jobs may run 6 hours (Sourcebook uses 50 minutes an hour) |

## 1. Database (Supabase)
1. Supabase > **New project** (your second free one), e.g. `kenuxa-sourcebook`. Save the database password.
2. **Connect** > **Session pooler**: copy the connection string (port 5432, `pooler.supabase.com`) and put the password in it.
   Use the *session* pooler, not the transaction pooler: Sourcebook sets its schema once per connection.
3. Nothing else: Sourcebook creates the `sourcebook` schema and its tables the first time it connects.

Space: my rough estimate is 5–10 KB per business with its sources, findings and search index, so 1,000 new businesses a day
is about 150–300 MB a month and the 500 MB lasts roughly 2–3 months. Supabase's dashboard shows the size (Reports > Database). When it gets close, turn the
world round down (Workspace > Crawler) or move to a paid plan.

## 2. Vercel (website)
1. Push this folder to a GitHub repository (public, for the free crawler in step 3). Your secrets stay in Vercel and GitHub's
   secret storage, never in the code.
2. Vercel > **Add New > Project** > import the repository. Project name `kenuxa-sourcebook` (the Kenuxa app menu points at
   `https://kenuxa-sourcebook.vercel.app`). Framework preset **Other**; `vercel.json` has the rest.
3. Environment variables:

| Name | Value |
|---|---|
| `DATABASE_URL` | the session pooler string from step 1 |
| `DATABASE_SCHEMA` | `sourcebook` |
| `SOURCEBOOK_PUBLIC_URL` | `https://kenuxa-sourcebook.vercel.app` |
| `SOURCEBOOK_SECRET` | long random text (`python -c "import secrets; print(secrets.token_urlsafe(48))"`) |
| `SOURCEBOOK_ADMIN_EMAILS` | your email |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | optional: "Continue with Google". Redirect URI `https://kenuxa-sourcebook.vercel.app/auth/callback` |
| `BRAVE_API_KEY` | optional: web search as an extra source |

4. Deploy, open the site, sign up with your admin email.

## 3. GitHub Actions (hourly crawler)
1. Move `deploy/sourcebook-crawler.yml` into a folder `.github/workflows/` (create it) and push.
2. Repository **Settings > Secrets and variables > Actions > New repository secret**:
   `SOURCEBOOK_DATABASE_URL` = the same connection string; `BRAVE_API_KEY` if you use it.
3. **Actions** tab > Sourcebook crawler > **Run workflow** once to check it. It then runs every hour at :23.

Each run queues the hour's work (monitored searches, crawl topics, the next countries in the world round, pages due for a
refresh), works through it for 50 minutes, finishes any search the website paused, and picks up anything Vercel stopped.
Once a day it commits `crawler-heartbeat.txt`: GitHub switches off schedules in public repositories after 60 days without a commit.

Private repository instead? GitHub gives 2,000 free minutes a month there, enough for one 50-minute run a day: change the
cron line in the workflow to `"23 2 * * *"`. That collects far fewer than 1,000 businesses a day.

## What works differently online
- A search shows results as they come in, as before. Big searches stop after 4 minutes with
  "Paused here to stay within the time limit"; the crawler checks the rest within the hour.
- Nothing runs between crawler runs, so a search started while nobody keeps the page open waits for the next run.
- Your computer (`Sourcebook.bat`) is unchanged and still runs everything itself, using `sourcebook.db`.
