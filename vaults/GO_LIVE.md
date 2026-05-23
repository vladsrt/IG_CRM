# IG CRM — GO LIVE (frontend on Vercel + backend local + ngrok)

> Goal: public link for your friend. Frontend = Vercel (public), backend =
> your machine, exposed via ngrok (https). Order matters: the ngrok URL must
> exist BEFORE the Vercel build (NEXT_PUBLIC_API_URL is baked at build time).

## 0. LLM key swap (now / in 3h)
`backend/.env` has two LLM blocks. **Now:** paste your temp key in the ACTIVE
(TEMP) block. **In ~3h:** comment the TEMP block's 3 lines, uncomment the MAIN
block's 3 lines, then restart the backend. (Last uncommented block wins.)
Restart = Ctrl+C in dev.sh, then `./dev.sh` (the API caches the key at startup).

## 1. Backend stack (local)
```bash
cd /home/sk8ver/Documents/Projects/CRM/IG_CRM
sudo docker compose up -d        # postgres (if not already up)
./dev.sh                          # api :8000 + worker + beat + web :3000
```
Check: `curl -s localhost:8000/health` → {"status":"ok"}

## 2. ngrok on the backend (port 8000)
```bash
# one-time: copy your authtoken from https://dashboard.ngrok.com → Your Authtoken
ngrok config add-authtoken <YOUR_NGROK_TOKEN>

# RECOMMENDED: claim a free Static Domain (dashboard → Domains) so the URL
# never changes between restarts:
ngrok http --url=<your-static>.ngrok-free.app 8000

# or quick one-off (URL changes each restart -> you'd have to redeploy Vercel):
ngrok http 8000
```
Copy the https URL it prints, e.g. `https://abc123.ngrok-free.app`.
(The frontend already sends `ngrok-skip-browser-warning`, so the free-tier
interstitial won't break API calls.)

## 3. Frontend → Vercel (uses your LOCAL code, no git push needed)
```bash
cd /home/sk8ver/Documents/Projects/CRM/IG_CRM/frontend/front_IG/instagram-crm-architecture
npx vercel login            # one-time
npx vercel link             # create/link a project (accept defaults)

# point the frontend at your ngrok backend URL (paste it when prompted):
npx vercel env add NEXT_PUBLIC_API_URL production
#   value: https://abc123.ngrok-free.app   (the ngrok URL from step 2)

npx vercel --prod           # build + deploy → prints your public site URL
```
Open the printed URL → that's the link to send your friend.

## 4. First login
The admin account already exists: **antivirys18@gmail.com / secret1234**.
Your friend registers their own; you grant them tier/agents in /admin.

## Keep running during the test
- `dev.sh` terminal (backend+worker+web) — leave open.
- `ngrok` terminal — leave open.
- Vercel stays up on its own.

## If ngrok URL changed (only if you DIDN'T use a static domain)
```bash
cd frontend/front_IG/instagram-crm-architecture
npx vercel env rm NEXT_PUBLIC_API_URL production   # then re-add with the new URL
npx vercel env add NEXT_PUBLIC_API_URL production
npx vercel --prod
```

## Troubleshooting
| Symptom | Fix |
|---|---|
| Vercel site loads but "Failed to fetch" / CORS | NEXT_PUBLIC_API_URL wrong or ngrok down; re-add env + redeploy |
| API returns HTML / "You are about to visit..." | ngrok interstitial — already handled by header; ensure you redeployed the latest frontend |
| AI chat 502 | LLM key (temp) wrong/exhausted — check backend/.env active block, restart dev.sh |
| Login works locally but not on Vercel | backend or ngrok not running, or env not set at build → redeploy |
| ngrok "ERR_NGROK_..." auth | run `ngrok config add-authtoken <token>` |
