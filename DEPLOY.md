# Putting ELD Checker online (free)

Two people signing in, no cost. About 20 minutes end to end.

## Before you push to GitHub

**Rotate the Google key first.** `service_account.json` has been sitting in an
unprotected folder, so treat it as compromised regardless of what happens next:

1. Google Cloud Console → IAM & Admin → Service Accounts → the account this app uses
2. Keys → delete the old key → **Add key → Create new key → JSON**
3. Save the download over `service_account.json`

**Then check nothing secret is staged.** These four must never be committed:

| File | Why |
|---|---|
| `service_account.json` | Google private key — full write access to the sheet |
| `roster.json` | 12 real driver names and unit numbers |
| `config.json` | Your sheet ID |
| `*.pdf` | Logbooks contain driver names, locations, licence numbers |

All four are in `.gitignore`. Verify before the first push:

```bash
git add -A && git status --porcelain
```

If any of those four appear in that list, **stop** and fix `.gitignore` first.

Prefer a **private** repo. The code is safe to publish, but a public repo invites
someone to fork it and it makes the project's purpose (and your carrier) public.

## Deploy on Render (free tier)

1. Push the repo to GitHub.
2. Sign up at render.com and pick **New → Web Service**, then connect the repo.
3. Settings: Runtime **Python**, Build `pip install -r requirements.txt`,
   Start `gunicorn app:app` (the `Procfile` already says this). Instance type **Free**.
4. Add these environment variables:

| Name | Value |
|---|---|
| `ELD_USERS` | `you:yourpassword,teammate:theirpassword` |
| `ELD_SECRET_KEY` | any long random string |
| `ELD_SHEET_ID` | the sheet ID from its URL |
| `GOOGLE_CREDENTIALS_JSON` | the **entire contents** of `service_account.json`, pasted as one value |

5. Deploy. You get an `https://…onrender.com` URL. Share it and the second
   username/password with your teammate.

`roster.json` isn't in the repo, so either add it through Render's Secret Files or
commit a version with the real names once the repo is private.

### Free-tier caveat

Render's free service sleeps after ~15 minutes idle, so the first visit of the day
takes roughly 50 seconds to wake. Every visit after that is instant. For a
once-a-morning tool that is usually fine — if it annoys you, PythonAnywhere's free
tier doesn't sleep but has tighter CPU limits.

## Security notes

- `ELD_USERS` is the only thing between the internet and your sheet. Use real
  passwords, not `admin123`.
- With no `ELD_USERS` set, the app **refuses all remote requests** rather than
  serving an open door. That is deliberate — a deploy that forgets the variable
  fails closed.
- Uploads are capped at 25 MB.
- Never commit the key. If it leaks, rotate it immediately using the steps above.
