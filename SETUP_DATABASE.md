# Setting up the AI Bridge database (Supabase)

This moves chats, memory, usage and attached files from local files into a
PostgreSQL database hosted on Supabase, so nothing is lost when the app
redeploys or restarts. About 15 minutes, one time.

## 1. Create the Supabase project
1. Go to https://supabase.com and sign up (GitHub login works).
2. Click **New project**.
   - Name: `ai-bridge`
   - **Database password:** click *Generate a password* and save it in your
     password manager. You need it in step 2 and can't view it again later
     (you can only reset it).
   - Region: Streamlit Community Cloud runs in the US, so a **US East** region
     is fastest. Pick **Central EU (Frankfurt)** if you'd rather keep the data
     in the EU (it works, each click is just slightly slower).
3. Click **Create new project** and wait a minute or two.

## 2. Copy the connection string
1. In your project, click the **Connect** button at the top of the page.
2. Under the connection string options, choose **Session pooler**.
   (Not "Direct connection": that one only works over IPv6, which Streamlit
   Cloud doesn't support.)
3. Copy the URI. It looks like:
   `postgresql://postgres.abcdefgh:[YOUR-PASSWORD]@aws-0-us-east-1.pooler.supabase.com:5432/postgres`
4. Replace `[YOUR-PASSWORD]` (including the brackets) with your database password.
   If the password contains special characters like `@ # / ? %`, either reset it
   to one with only letters and numbers, or URL-encode them (`@` becomes `%40`).

## 3. Run it locally first
1. Open `D:\AI-Bridge\.env` and add a line:
   ```
   DATABASE_URL=postgresql://postgres.abcdefgh:yourpassword@aws-0-us-east-1.pooler.supabase.com:5432/postgres
   ```
2. In a terminal in `D:\AI-Bridge`, with your virtual environment active:
   ```
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```
3. Copy your existing chats into the database (safe to run twice):
   ```
   python migrate_json.py
   ```
4. Start the app: `streamlit run app.py`
5. Check that your old chat shows up, ask a question, then stop and restart
   the app. The chat and the Usage totals should still be there.
6. Optional: in Supabase, open **Table Editor** to see the rows (`chats`,
   `messages`, `usage_log`, ...). The app created the tables itself.

## 4. Add the secret on Streamlit Cloud
1. Go to https://share.streamlit.io, open your app's **⋮** menu, then **Settings → Secrets**.
2. Add this line under your existing secrets (keep the quotes):
   ```
   DATABASE_URL = "postgresql://postgres.abcdefgh:yourpassword@aws-0-us-east-1.pooler.supabase.com:5432/postgres"
   ```
3. Click **Save**.

Do this before pushing the code. Otherwise the hosted app will show
"Couldn't connect to the database" until the secret is added.

## 5. Push the code
```
cd D:\AI-Bridge
git add .
git commit -m "Store chats, memory and usage in a database"
git push
```
Streamlit Cloud redeploys on its own. Open the app and check that your chats are there.

## 6. Optional: keep original files, not just their text
The text of every attached file is always saved. To also keep the original
PDF/Word file:
1. In Supabase: **Storage → New bucket**, name it `files`, leave *Public* off.
2. **Project Settings → API Keys**: copy the **service_role** (secret) key.
   Never put this key in code or on GitHub.
3. Add to `.env` and to Streamlit Secrets:
   ```
   SUPABASE_URL=https://abcdefgh.supabase.co
   SUPABASE_SERVICE_KEY=the-service-role-key
   ```
   (In Streamlit Secrets, use `KEY = "value"` with quotes, like in step 4.)

## What's new in the app
- **Chats survive** redeploys and restarts, and are the same on every device.
- **Search** box in the sidebar searches titles *and* message text.
- **Pin** (top right of a chat) and **Rename chat**.
- **Memory** in the sidebar: things both models are told on every question,
  either in all chats or in this chat only.
- **Usage** shows today / this chat / all time, per model, and keeps counting
  across restarts.

## Good to know
- **Free tier pausing:** Supabase pauses free projects after about a week
  without activity. If the app says it can't connect, open the Supabase
  dashboard and click **Restore project**.
- **Backups:** don't rely on the free tier for backups.
  To make your own occasionally, run:
  `pg_dump "your-DATABASE_URL" > backup.sql`
- **Moving hosts later:** the app only needs `DATABASE_URL`, so any Postgres
  host works (Neon, Railway, your own server). Export with `pg_dump`, import
  there, and change the one setting.
- The old `chats/` folder and `history.json` are no longer used. Once you've
  checked everything migrated, you can delete them.
