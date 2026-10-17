from __future__ import annotations
"""Local mode (NFC_MODE=local): the PN532 is attached to the same PC that runs the web app.
Uses the same PN532 driver as the Local NFC Bridge (local_bridge/pn532_raw.py), so UID reading and
NDEF writing for NTAG213/215/216 behave identically in both modes. Public API is unchanged."""
import os, threading, time, uuid

DEBOUNCE_SECONDS=3.0
_reader=None
_lock_start=threading.Lock()
_state_lock=threading.Lock()
_state={'available':False,'status':'SCANNING PORTS...','port':None,'last_uid':None,'last_seen':None,'last_error':None,'event_id':0,'pairing':False,'pair_uid':None,'pair_event_id':None,'pair_token':None,'write_status':'IDLE','write_uid':None,'write_url':None,'write_error':None,'write_event_id':None}
_last_open={'uid':None,'time':0.0}
_write={'request_id':None,'event':None}; _write_counter=0

def get_state():
    with _state_lock:return dict(_state)
def _set(**kw):
    with _state_lock:_state.update(kw)

def _on_state(available,status,port,error):
    _set(available=available,status=status,port=port,last_error=error)

def _on_tag(uid):
    now=time.time()
    # Read pairing state at the exact moment the tag is detected (avoids a race with the browser starting pairing).
    with _state_lock:
        pairing_now=bool(_state.get('pairing',False))
        event_id=int(_state.get('event_id',0))+1
    if uid==_last_open['uid'] and now-_last_open['time']<DEBOUNCE_SECONDS and not pairing_now:
        return
    _last_open.update({'uid':uid,'time':now})
    _set(last_uid=uid,last_seen=time.strftime('%Y-%m-%d %H:%M:%S'),event_id=event_id)
    if pairing_now:
        _set(pair_uid=uid,pair_event_id=event_id,pairing=False)
        print(f'[NFC PAIR] UID={uid}')

def _on_write_result(request_id,ok,err,uid):
    with _state_lock:
        if _write.get('request_id')!=request_id: return      # stale / superseded request
        ev=_write['event']; _write.update({'request_id':None,'event':None})
        _state.update({'write_status':'SUCCESS' if ok else 'ERROR','write_error':None if ok else str(err or 'write failed')[:300],'last_uid':uid,'last_seen':time.strftime('%Y-%m-%d %H:%M:%S')})
    print(f"[NFC WRITE] {'OK' if ok else 'ERROR '+str(err)} {uid}")
    if ev: ev.set()

def start_nfc_reader(base_url:str):
    global _reader
    with _lock_start:
        if _reader and _reader.thread and _reader.thread.is_alive(): return
        try:
            from local_bridge.bridge_reader import Pn532Reader
        except Exception as exc:
            _set(status='NFC LIBRARY NOT INSTALLED',last_error=str(exc),available=False,port=None); return
        _reader=Pn532Reader(_on_tag,_on_state,_on_write_result,com_port=os.environ.get('COM_PORT','auto'),backend=os.environ.get('READER_BACKEND','raw'),baud=int(os.environ.get('BAUD') or 115200))
        _reader.start()
def stop_nfc_reader():
    if _reader: _reader.stop()
def start_pairing():
    # Start a fresh one-shot pairing session. Reset the debounce marker so a tag that was just read can be captured immediately.
    _last_open.update({'uid':None,'time':0.0})
    with _state_lock:_state.update({'pairing':True,'pair_uid':None,'pair_event_id':None,'pair_token':None})
    if _reader: _reader.rescan()
def cancel_pairing():
    with _state_lock:_state.update({'pairing':False,'pair_uid':None,'pair_event_id':None,'pair_token':None})
def request_nfc_url_write(uid,target_url,wait_seconds=8.0):
    global _write_counter
    ev=threading.Event(); normalized=str(uid or '').strip().upper(); url=str(target_url or '').strip(); rid=uuid.uuid4().hex
    with _state_lock:
        _write_counter+=1; _write.update({'request_id':rid,'event':ev})
        _state.update({'write_status':'WAITING_FOR_TAG','write_uid':normalized,'write_url':url,'write_error':None,'write_event_id':_write_counter})
    if _reader: _reader.request_write(rid,normalized,url,max(60.0,float(wait_seconds)))
    ev.wait(max(0,float(wait_seconds)))
    return get_state()
