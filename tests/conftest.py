import importlib, os, sys, tempfile, pytest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_pg = None
def _pg_url():
    """Real PostgreSQL for tests: TEST_DATABASE_URL, or an embedded server from `pip install pgserver`."""
    global _pg
    if os.environ.get('TEST_DATABASE_URL'):
        return os.environ['TEST_DATABASE_URL']
    try:
        import pgserver
    except ImportError:
        return None
    if _pg is None:
        _pg = pgserver.get_server(tempfile.mkdtemp(prefix='stk_pg_'))
    return _pg.get_uri()

def make_app(backend, monkeypatch, **env):
    base = {'NFC_MODE': 'bridge', 'BRIDGE_TOKEN': 'x' * 40, 'ALLOW_SIMULATED_BRIDGE': '1', 'DATABASE_URL': '',
            'APP_PASSWORD': '', 'BASE_URL': 'https://study.example.com', 'STUDYTOKEN_DB': ''}
    base.update(env)
    if backend == 'pg':
        url = _pg_url()
        if not url:
            pytest.skip('no PostgreSQL available (pip install pgserver or set TEST_DATABASE_URL)')
        base['DATABASE_URL'] = url
    else:
        base['STUDYTOKEN_DB'] = os.path.join(tempfile.mkdtemp(), 't.db')
    for k, v in base.items():
        monkeypatch.setenv(k, v)
    for m in ('database', 'nfc_hub', 'bridge_api', 'security', 'app'):
        sys.modules.pop(m, None)
    import database
    if backend == 'pg':   # clean slate
        c = database.connect()
        c.execute('DROP TABLE IF EXISTS timeline_sections'); c.execute('DROP TABLE IF EXISTS sessions')
        c.execute('DROP TABLE IF EXISTS courses'); c.execute('DROP TABLE IF EXISTS subjects'); c.commit(); c.close()
    import app as appmod
    appmod.app.config['TESTING'] = True
    return appmod

@pytest.fixture(params=['sqlite', 'pg'])
def appmod(request, monkeypatch):
    return make_app(request.param, monkeypatch)

@pytest.fixture
def client(appmod):
    return appmod.app.test_client()
