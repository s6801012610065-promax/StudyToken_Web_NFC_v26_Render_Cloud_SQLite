import os
bind = f"0.0.0.0:{os.environ.get('PORT', '10000')}"
# ONE process, many threads: the NFC hub (bridge state, pairing, pending writes) lives in memory.
workers = 1
worker_class = 'gthread'
threads = int(os.environ.get('WEB_THREADS', '32'))   # a long-poll from the bridge occupies one thread
timeout = 120
graceful_timeout = 8      # open long-polls from the bridge must not delay a redeploy; the bridge reconnects by itself
keepalive = 75
accesslog = '-'
errorlog = '-'
loglevel = os.environ.get('LOG_LEVEL', 'info').lower()
