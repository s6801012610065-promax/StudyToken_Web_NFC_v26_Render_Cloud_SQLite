from __future__ import annotations
import threading,time

MAX_PORT=30
DEBOUNCE_SECONDS=3.0
_reader_thread=None
_stop_event=threading.Event()
_state_lock=threading.Lock()
_state={'available':False,'status':'SCANNING PORTS...','port':None,'last_uid':None,'last_seen':None,'last_error':None,'event_id':0,'pairing':False,'pair_uid':None,'pair_event_id':None,'pair_token':None,'write_status':'IDLE','write_uid':None,'write_url':None,'write_error':None,'write_event_id':None}
_last_open={'uid':None,'time':0.0}
_write_request={'uid':None,'url':None,'event':None}; _write_counter=0

def get_state():
    with _state_lock:return dict(_state)
def _set(**kw):
    with _state_lock:_state.update(kw)
def start_nfc_reader(base_url:str):
    global _reader_thread
    if _reader_thread and _reader_thread.is_alive(): return
    _stop_event.clear(); _reader_thread=threading.Thread(target=_worker,args=(base_url,),daemon=True); _reader_thread.start()
def stop_nfc_reader(): _stop_event.set()
def start_pairing():
    # Start a fresh one-shot pairing session. Reset the debounce marker so a
    # tag that was just read can be captured immediately after Save Course.
    _last_open.update({'uid':None,'time':0.0})
    with _state_lock:_state.update({'pairing':True,'pair_uid':None,'pair_event_id':None,'pair_token':None})
def cancel_pairing():
    with _state_lock:_state.update({'pairing':False,'pair_uid':None,'pair_event_id':None,'pair_token':None})
def request_nfc_url_write(uid,target_url,wait_seconds=8.0):
    global _write_counter
    ev=threading.Event(); normalized=str(uid or '').strip().upper()
    with _state_lock:
        _write_counter+=1; _write_request.update({'uid':normalized,'url':str(target_url or '').strip(),'event':ev})
        _state.update({'write_status':'WAITING_FOR_TAG','write_uid':normalized,'write_url':_write_request['url'],'write_error':None,'write_event_id':_write_counter})
    ev.wait(max(0,float(wait_seconds)))
    return get_state()

def _try_open(nfc,port):
    try:
        clf=nfc.ContactlessFrontend(f'com:COM{port}:pn532')
        return clf,None
    except Exception as e:
        return None,str(e)

def _find_reader(nfc):
    """Scan COM1..COM30 in order and return the first working PN532."""
    for port in range(1,MAX_PORT+1):
        if _stop_event.is_set():
            return None,None
        candidate,_=_try_open(nfc,port)
        if candidate:
            return candidate,port
    return None,None

def _worker(base_url):
    try:
        import nfc
    except Exception as exc:
        _set(status='NFC LIBRARY NOT INSTALLED',last_error=str(exc),available=False,port=None)
        return

    clf=None
    try:
        while not _stop_event.is_set():
            # Keep scanning forever. This makes plugging/unplugging the PN532
            # work without restarting Flask or refreshing the browser.
            if clf is None:
                _set(available=False,status='SEARCHING FOR NFC READER...',port=None,last_error=None)
                clf,port=_find_reader(nfc)
                if clf:
                    _set(available=True,status='READY',port=f'COM{port}',last_error=None)
                    continue
                _set(available=False,status='WAITING FOR NFC READER',port=None,last_error=None)
                _stop_event.wait(1.5)
                continue

            def on_connect(tag):
                uid=tag.identifier.hex(':').upper()
                now=time.time()
                state=get_state()
                with _state_lock:
                    pending_uid=_write_request.get('uid')
                    pending_url=_write_request.get('url')
                    pending_event=_write_request.get('event')
                if pending_uid and uid==pending_uid and pending_url:
                    try:
                        import ndef
                        if tag.ndef is None:
                            raise RuntimeError('Tag is not NDEF formatted or does not support NDEF')
                        if not tag.ndef.is_writeable:
                            raise RuntimeError('Tag is locked and cannot be written')
                        rec=ndef.UriRecord(pending_url)
                        try:
                            tag.ndef.records=[rec]
                        except Exception as first:
                            try:
                                empty=int(tag.ndef.length)==0
                            except Exception:
                                empty=False
                            if not empty:
                                raise first
                            if tag.ndef.format() is False:
                                raise first
                            tag.ndef.records=[rec]
                        _set(write_status='SUCCESS',write_error=None,last_uid=uid,last_seen=time.strftime('%Y-%m-%d %H:%M:%S'))
                        print(f'[NFC WRITE] {pending_url} -> {uid}')
                    except Exception as exc:
                        _set(write_status='ERROR',write_error=str(exc),last_uid=uid,last_seen=time.strftime('%Y-%m-%d %H:%M:%S'))
                        print(f'[NFC WRITE] ERROR {uid}: {exc}')
                    finally:
                        with _state_lock:
                            _write_request.update({'uid':None,'url':None,'event':None})
                        if pending_event:
                            pending_event.set()
                    return False

                # Read pairing state at the exact moment the tag is detected.
                # This avoids a race where the browser starts pairing just after
                # the reader callback captured an older state snapshot.
                with _state_lock:
                    pairing_now=bool(_state.get('pairing',False))
                    event_id=int(_state.get('event_id',0))+1
                if uid==_last_open['uid'] and now-_last_open['time']<DEBOUNCE_SECONDS and not pairing_now:
                    return False
                _last_open.update({'uid':uid,'time':now})
                _set(last_uid=uid,last_seen=time.strftime('%Y-%m-%d %H:%M:%S'),event_id=event_id)
                if pairing_now:
                    _set(pair_uid=uid,pair_event_id=event_id,pairing=False)
                    print(f'[NFC PAIR] UID={uid}')
                return False

            try:
                clf.connect(rdwr={'on-connect':on_connect},terminate=lambda:_stop_event.is_set())
                # A normal connect() return can happen when the reader is
                # disconnected or the polling loop ends; verify it again.
                if not _stop_event.is_set():
                    _set(available=False,status='SEARCHING FOR NFC READER...',port=None,last_error=None)
                    try:
                        clf.close()
                    except Exception:
                        pass
                    clf=None
            except Exception as exc:
                print(f'[NFC] Reader disconnected/error: {exc}')
                _set(available=False,status='SEARCHING FOR NFC READER...',port=None,last_error=None)
                try:
                    clf.close()
                except Exception:
                    pass
                clf=None
                _stop_event.wait(0.5)
    finally:
        if clf:
            try:
                clf.close()
            except Exception:
                pass
        _set(available=False,status='STOPPED',port=None,last_error=None)

