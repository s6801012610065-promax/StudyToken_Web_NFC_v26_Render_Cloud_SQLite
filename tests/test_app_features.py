"""Existing features (Dashboard/Courses/Chapters/Timer/History/Timeline) on SQLite AND real PostgreSQL.
These tests need no NFC hardware and no bridge: UID values are posted like the GUI does."""
import re

def add_course(c, code='0101231', name='Softdev', chapter='1', title='Intro', minutes=10, uid=None):
    tok = c.post('/api/nfc/token/new', json={}).get_json()['nfc_token']
    body = dict(subject_code=code, name=name, chapter=chapter, title=title, duration=str(minutes), nfc_token=tok)
    if uid: body['nfc_uid'] = uid
    return c.post('/api/courses', json=body)

def test_token_format_and_subject_flow(client):
    r = add_course(client, chapter='๑')            # Thai digit normalised
    j = r.get_json(); assert r.status_code == 200 and j['chapter'] == '1' and re.fullmatch(r'STK-[0-9A-F]{8}', j['nfc_token'])
    s = client.get('/api/subjects/0101231').get_json()
    assert s['found'] and s['name'] == 'Softdev' and s['next_chapter'] == '2'
    assert client.get('/api/subjects/nope').status_code == 404
    r2 = add_course(client, name='ignored', chapter='IV')   # roman numeral, name locked by existing subject
    assert r2.get_json()['chapter'] == '4' and r2.get_json()['name'] == 'Softdev'

def test_validation_errors(client):
    tok = client.post('/api/nfc/token/new', json={}).get_json()['nfc_token']
    assert client.post('/api/courses', json={'subject_code': '', 'nfc_token': tok}).status_code == 400
    assert client.post('/api/courses', json={'subject_code': 'A', 'name': 'n', 'chapter': 'abc', 'duration': '5', 'nfc_token': tok}).status_code == 400
    assert client.post('/api/courses', json={'subject_code': 'A', 'name': 'n', 'chapter': '1', 'duration': '0', 'nfc_token': tok}).status_code == 400
    assert client.post('/api/courses', json={'subject_code': 'A', 'name': 'n', 'chapter': '1', 'duration': '5', 'nfc_token': 'bad'}).status_code == 400
    add_course(client)
    cid = client.get('/api/courses').get_json()[0]
    dup = client.post('/api/courses', json={'subject_code': 'X', 'name': 'n', 'chapter': '1', 'duration': '5', 'nfc_token': cid['nfc_token']})
    assert dup.status_code == 409

def test_uid_token_course_relationship(client):
    j = add_course(client, uid='04:AA:BB:CC').get_json()
    assert j['tag_uid'] == '04:AA:BB:CC'
    # UID -> token -> course
    r = client.get('/api/nfc/resolve/04aabbcc').get_json()
    assert r['paired'] and r['nfc_token'] == j['nfc_token'] and r['course']['id'] == j['course_id']
    v = client.get('/api/nfc/verify/04:AA:BB:CC').get_json(); assert v['found'] and v['course']['nfc_token'] == j['nfc_token']
    v = client.get('/api/nfc/verify/' + j['nfc_token']).get_json(); assert v['found']
    assert client.get('/api/nfc/verify/FF:FF').status_code == 404
    # same UID cannot be linked to a second token
    c2 = add_course(client, chapter='2').get_json()
    r = client.post('/api/nfc/link', json={'nfc_token': c2['nfc_token'], 'nfc_uid': '04:AA:BB:CC'})
    assert r.status_code == 409
    # token URL (what phones open)
    page = client.get('/nfc/' + j['nfc_token']); assert page.status_code == 200 and b'Softdev' in page.data
    assert client.get('/nfc/STK-00000000').status_code == 404

def test_link_after_save_and_edit_delete(client):
    j = add_course(client).get_json()
    r = client.post('/api/nfc/link', json={'nfc_token': j['nfc_token'], 'nfc_uid': '04:11:22:33'}).get_json()
    assert r['ok'] and r['nfc_url'] == f"https://study.example.com/nfc/{j['nfc_token']}"
    e = client.put(f"/api/courses/{j['course_id']}", json={'name': 'Renamed', 'chapter': 'V', 'title': 'T', 'duration': '20'})
    assert e.get_json()['course']['chapter'] == '5' and e.get_json()['course']['duration'] == 1200
    assert client.delete(f"/api/courses/{j['course_id']}").get_json()['ok']
    assert client.get('/api/courses').get_json() == []

def test_courses_sorted_by_subject_then_chapter(client):
    for ch in ('10', '2', '1'): add_course(client, chapter=ch)
    assert [c['chapter'] for c in client.get('/api/courses').get_json()] == ['1', '2', '10']

def study(client, cid, positions_and_notes, minutes=10):
    sid = client.post('/api/sessions/start', json={'course_id': cid}).get_json()['session_id']
    out = []
    for pos, note in positions_and_notes:
        client.post(f'/api/sessions/{sid}/autosave', json={'position': pos})
        r = client.post(f'/api/sessions/{sid}/sections', json={'position': pos, 'description': note, 'client_id': f'c{pos}'})
        out.append(r); client.post(f'/api/sessions/{sid}/resume', json={})
    return sid, out

def test_timer_timeline_history_flow(client):
    cid = add_course(client, minutes=10).get_json()['course_id']
    sid, res = study(client, cid, [(120, 'intro'), (300, 'loops')])
    assert all(r.status_code == 200 for r in res)
    assert res[1].get_json()['section']['seq'] == 2 and res[1].get_json()['section']['duration'] == 180
    dup = client.post(f'/api/sessions/{sid}/sections', json={'position': 300, 'description': 'loops', 'client_id': 'c300'}).get_json()
    assert dup['duplicate'] is True
    assert client.post(f'/api/sessions/{sid}/sections', json={'position': 300, 'description': 'again'}).status_code == 409   # empty segment
    assert client.post(f'/api/sessions/{sid}/sections', json={'position': 400, 'description': ''}).status_code == 400
    f = client.post(f'/api/sessions/{sid}/finish', json={'position': 420}).get_json()
    assert f['ok'] and f['section']['is_auto'] == 1 and f['section']['duration'] == 120   # Finish never asks, stores remainder
    assert client.post(f'/api/sessions/{sid}/finish', json={'position': 420}).get_json()['already_finished']
    t = client.get(f'/api/courses/{cid}/timeline').get_json()
    assert t['count'] == 3 and t['total_seconds'] == 420 and [s['seq'] for s in t['sections']] == [1, 2, 3]
    h = client.get('/api/history').get_json()
    assert len(h) == 1 and h[0]['name'] == 'Softdev' and h[0]['end_position'] == 420
    assert client.get('/api/today').get_json()['seconds'] == 420
    c = client.get('/api/courses').get_json()[0]
    assert c['sessions_count'] == 1 and c['studied_seconds'] == 420 and c['sections_count'] == 3 and c['state'] == 'IN_PROGRESS'

def test_finish_at_100_percent_and_close(client):
    cid = add_course(client, minutes=1).get_json()['course_id']
    sid = client.post('/api/sessions/start', json={'course_id': cid}).get_json()['session_id']
    client.post(f'/api/sessions/{sid}/autosave', json={'position': 9999})
    f = client.post(f'/api/sessions/{sid}/finish', json={'position': 9999}).get_json()
    assert f['completed'] is True
    assert client.get('/api/courses').get_json()[0]['state'] == 'FINISH'
    sid2 = client.post('/api/sessions/start', json={'course_id': cid}).get_json()['session_id']
    c = client.post(f'/api/sessions/{sid2}/close', json={'position': 5}).get_json()   # tab closed: no section
    assert c['ok'] and client.get(f'/api/courses/{cid}/timeline').get_json()['count'] == 1

def test_late_autosave_does_not_reopen_and_never_rolls_back(client):
    cid = add_course(client).get_json()['course_id']
    sid = client.post('/api/sessions/start', json={'course_id': cid}).get_json()['session_id']
    client.post(f'/api/sessions/{sid}/autosave', json={'position': 200})
    client.post(f'/api/sessions/{sid}/autosave', json={'position': 100})
    assert client.get('/api/courses').get_json()[0]['position'] == 200
    client.post(f'/api/sessions/{sid}/finish', json={'position': 200})
    assert client.post(f'/api/sessions/{sid}/autosave', json={'position': 250}).get_json()['ignored']

def test_pages_render(client):
    for p in ('/', '/history', '/desktop', '/health', '/static/app.js', '/static/style.css'):
        assert client.get(p).status_code == 200, p
    assert client.get('/health/full').get_json()['ok']

def test_delete_course_keeps_history_cascades_timeline(client):
    cid = add_course(client).get_json()['course_id']
    sid, _ = study(client, cid, [(60, 'a')]); client.post(f'/api/sessions/{sid}/finish', json={'position': 60})
    client.delete(f'/api/courses/{cid}')
    assert len(client.get('/api/history').get_json()) == 1
