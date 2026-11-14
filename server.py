"""kosync-compatible server for KOReader that mirrors progress into Moon+ Reader's .po sync files."""
import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
import time
from contextlib import closing
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from epub import Book, partial_md5, title_key

USER = os.environ['SYNC_USER']
KEY = hashlib.md5(os.environ['SYNC_PASSWORD'].encode()).hexdigest()  # KOReader sends md5(password)
LIBRARY = os.environ.get('LIBRARY_DIR', '/books')
MOON_DIR = os.environ.get('MOON_DIR', '/moon')
DB = os.environ.get('DB_PATH', '/data/sync.db')
POLL = float(os.environ.get('POLL_SECONDS', '10'))
PORT = int(os.environ.get('PORT', '8081'))

PO_RE = re.compile(r'^(\d+)\*(\d+)@(\d+)#(\d+):([\d.]+)%')


def moon_key(po_name):
    """'Title - Author.epub.po' -> title key."""
    return title_key(po_name[:-len('.epub.po')].rsplit(' - ', 1)[0])


class Library:
    def __init__(self):
        self.lock = threading.Lock()
        self.by_key, self.by_doc, self.po_for_key = {}, {}, {}
        self._seen = {}  # path -> (mtime, key, md5)

    def scan(self):
        by_key, by_doc = {}, {}
        for root, _, files in os.walk(LIBRARY):
            for f in files:
                if not f.lower().endswith('.epub'):
                    continue
                path = os.path.join(root, f)
                try:
                    mtime = os.stat(path).st_mtime
                    if self._seen.get(path, (None,))[0] != mtime:
                        self._seen[path] = (mtime, title_key(Book(path).title), partial_md5(path))
                except Exception as e:  # one bad EPUB must not stop the scan
                    print(f'skip {path}: {e}', flush=True)
                    continue
                _, key, md5 = self._seen[path]
                by_key.setdefault(key, path)
                by_doc[md5] = key
        po_for_key = {}
        for po in sorted(self.po_files(), key=lambda p: os.stat(os.path.join(MOON_DIR, p)).st_mtime):
            po_for_key[moon_key(po)] = po  # newest wins when two filenames share a title
            by_doc[hashlib.md5(po[:-3].encode()).hexdigest()] = moon_key(po)  # KOReader "filename" matching
        with self.lock:
            self.by_key, self.by_doc, self.po_for_key = by_key, by_doc, po_for_key

    @staticmethod
    def po_files():
        return [f for f in os.listdir(MOON_DIR) if f.endswith('.epub.po')]

    def book(self, key):
        with self.lock:
            path = self.by_key.get(key)
        return _load_book(path, os.stat(path).st_mtime) if path else None


@lru_cache(maxsize=16)
def _load_book(path, mtime):
    return Book(path)


LIB = Library()


def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute('''CREATE TABLE IF NOT EXISTS progress (
        key TEXT PRIMARY KEY, source TEXT, progress TEXT, percentage REAL,
        device TEXT, device_id TEXT, timestamp INTEGER, po TEXT)''')
    return conn


def save(conn, **row):
    conn.execute('INSERT OR REPLACE INTO progress VALUES (:key,:source,:progress,:percentage,:device,:device_id,:timestamp,:po)', row)
    conn.commit()


def import_moon_files(conn):
    """Store Moon+ positions newer than what we hold."""
    for po in LIB.po_files():
        path = os.path.join(MOON_DIR, po)
        try:
            with open(path) as f:
                content = f.read().strip()
            mtime = int(os.stat(path).st_mtime)
        except OSError:
            continue
        m = PO_RE.match(content)
        key = moon_key(po)
        row = conn.execute('SELECT po, timestamp FROM progress WHERE key=?', (key,)).fetchone()
        if not m or (row and (row['po'] == content or row['timestamp'] > mtime)):
            continue
        save(conn, key=key, source='moon', progress=content, percentage=float(m.group(5)) / 100,
             device='Moon+ Reader', device_id='moonreader', timestamp=mtime, po=content)


def write_moon(key, xpointer, percentage):
    """Mirror a KOReader position into Moon+'s .po file; returns the text written or None."""
    with LIB.lock:
        po = LIB.po_for_key.get(key)
    book = LIB.book(key)
    if not po or not book:
        return None  # shortcut: Moon+ creates the .po on first open; until then we have no filename to write
    pos = book.xpointer_to_moon(xpointer)
    if not pos:
        return None
    path = os.path.join(MOON_DIR, po)
    with open(path) as f:
        m = PO_RE.match(f.read().strip())
    prefix = m.group(1) if m else '0'  # constant per install; keep whatever Moon+ wrote
    content = f'{prefix}*{pos[0]}@0#{pos[1]}:{percentage * 100:.1f}%'
    with open(path, 'w') as f:
        f.write(content)
    return content


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def authed(self):
        ok = self.headers.get('x-auth-user') == USER and hmac.compare_digest(self.headers.get('x-auth-key', ''), KEY)
        if not ok:
            self.reply(401, {'code': 2001, 'message': 'Unauthorized'})
        return ok

    def body(self):
        try:
            return json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
        except ValueError:
            return None

    def resolve(self, document):
        with LIB.lock:
            return LIB.by_doc.get(document, 'doc:' + document)

    def do_GET(self):
        if self.path == '/healthcheck':
            return self.reply(200, {'state': 'OK'})
        if self.path == '/users/auth':
            return self.authed() and self.reply(200, {'authorized': 'OK'})
        m = re.fullmatch(r'/syncs/progress/([^/]+)', self.path)
        if not m:
            return self.reply(404, {'message': 'Not found'})
        if not self.authed():
            return
        document = m.group(1)
        key = self.resolve(document)
        with closing(db()) as conn:
            row = conn.execute('SELECT * FROM progress WHERE key=?', (key,)).fetchone()
        if not row:
            return self.reply(200, {})
        out = {'document': document, 'percentage': row['percentage'], 'device': row['device'],
               'device_id': row['device_id'], 'timestamp': row['timestamp'], 'progress': row['progress']}
        if row['source'] == 'moon':
            book = LIB.book(key)
            pm = PO_RE.match(row['progress'])
            if not book:
                return self.reply(200, {})  # no EPUB to translate against
            out['progress'], _ = book.moon_to_xpointer(int(pm.group(2)), int(pm.group(4)))
        self.reply(200, out)

    def do_PUT(self):
        if self.path != '/syncs/progress':
            return self.reply(404, {'message': 'Not found'})
        if not self.authed():
            return
        b = self.body()
        if not isinstance(b, dict) or not b.get('document') or not isinstance(b.get('progress'), str) \
                or not isinstance(b.get('percentage'), (int, float)):
            return self.reply(403, {'code': 2003, 'message': 'Invalid request'})
        key = self.resolve(b['document'])
        now = int(time.time())
        try:
            po = write_moon(key, b['progress'], b['percentage'])
        except Exception as e:  # never lose KOReader's update because the Moon+ mirror failed
            print(f'moon write failed for {key}: {e}', flush=True)
            po = None
        with closing(db()) as conn:
            save(conn, key=key, source='ko', progress=b['progress'], percentage=b['percentage'],
                 device=str(b.get('device', '')), device_id=str(b.get('device_id', '')), timestamp=now, po=po)
        self.reply(200, {'document': b['document'], 'timestamp': now})

    def do_POST(self):
        if self.path != '/users/create':
            return self.reply(404, {'message': 'Not found'})
        b = self.body() or {}
        # single configured user: "Register" succeeds only with the configured credentials
        if b.get('username') == USER and hmac.compare_digest(str(b.get('password', '')), KEY):
            return self.reply(201, {'username': USER})
        self.reply(402, {'code': 2005, 'message': 'User registration is disabled.'})


def watch():
    while True:
        try:
            LIB.scan()
            with closing(db()) as conn:
                import_moon_files(conn)
        except Exception as e:
            print(f'poll failed: {e}', flush=True)
        time.sleep(POLL)


if __name__ == '__main__':
    LIB.scan()
    threading.Thread(target=watch, daemon=True).start()
    print(f'listening on :{PORT}', flush=True)
    ThreadingHTTPServer(('', PORT), Handler).serve_forever()
