# StudyToken Web (NFC)

Flask + SQLite study timer with NFC token links and chapter timelines.
Original Thai notes: [README_TH.md](README_TH.md)

## Deploy on Render (free)
1. Push this folder to a GitHub repository.
2. Render dashboard -> **New +** -> **Blueprint** (uses `render.yaml`)
   or **Web Service** with:
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4`
3. Open your `https://<name>.onrender.com` URL.

### Free plan limits
- The service sleeps after ~15 min idle (first load takes ~30-60 s).
- The disk is **not persistent**: the SQLite database resets on every redeploy/restart.
  For permanent data, use a paid plan with a Disk mounted at `/var/data` and set env
  `STUDYTOKEN_DB=/var/data/study_token.db`.
- The PN532 USB reader cannot work on Render (no COM ports). Phones can still open
  `https://<name>.onrender.com/nfc/<TOKEN>` links from NFC tags.

## Run locally with PN532
```
pip install -r requirements-local.txt
python app.py
```
