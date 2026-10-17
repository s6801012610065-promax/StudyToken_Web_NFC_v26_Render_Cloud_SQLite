Run: `pip install pytest pgserver requests -r requirements.txt && pytest tests -q`
(`pgserver` provides an embedded real PostgreSQL; or set TEST_DATABASE_URL.) The e2e file takes ~2.5 minutes.
All NFC tests use a SIMULATED reader. Real PN532 testing = checklist in README_TH.md section 9.
