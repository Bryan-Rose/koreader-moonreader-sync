import hashlib
import json
import os
import tempfile
import threading
import unittest
import urllib.request
import zipfile

TMP = tempfile.mkdtemp()
os.environ.update(SYNC_USER='me', SYNC_PASSWORD='pw', LIBRARY_DIR=f'{TMP}/books', MOON_DIR=f'{TMP}/moon',
                  DB_PATH=f'{TMP}/sync.db', PORT='0')
os.makedirs(f'{TMP}/books')
os.makedirs(f'{TMP}/moon')

import server  # noqa: E402  (env must be set first)
from epub import Book, partial_md5  # noqa: E402

PARA = 'x' * 100


def make_epub(path):
    """front matter (not in TOC) + 3 chapters; chapter 2 spans two spine files."""
    ch = lambda n, extra='': f'<html><body><h1>Ch {n}</h1>{extra}' + f'<p>{PARA}</p>' * 5 + '<br/><img src="a"/></body></html>'
    files = {'front.xhtml': '<html><body><p>&copy; me</p></body></html>', 'c1.xhtml': ch(1),
             'c2a.xhtml': ch(2, '<div><p>intro</p></div>'), 'c2b.xhtml': ch('2b'), 'c3.xhtml': ch(3)}
    order = ['front', 'c1', 'c2a', 'c2b', 'c3']
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('META-INF/container.xml', '<container><rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
        z.writestr('OEBPS/content.opf', '<package><metadata><dc:title>Test: Book&#39;s Title</dc:title></metadata><manifest>'
                   + ''.join(f'<item id="{n}" href="{n}.xhtml"/>' for n in order)
                   + '<item id="ncx" href="toc.ncx"/></manifest><spine toc="ncx">'
                   + ''.join(f'<itemref idref="{n}"/>' for n in order) + '</spine></package>')
        z.writestr('OEBPS/toc.ncx', '<ncx>' + ''.join(f'<navPoint><content src="{n}.xhtml#x"/></navPoint>' for n in ['c1', 'c2a', 'c3']) + '</ncx>')
        for n, body in files.items():
            z.writestr(f'OEBPS/{n}', body)


EPUB = f'{TMP}/books/Some Author/book.epub'
os.makedirs(os.path.dirname(EPUB))
make_epub(EPUB)
PO_NAME = 'Test Books Title - Some Author.epub.po'


class EpubTest(unittest.TestCase):
    def test_segments_and_roundtrip(self):
        b = Book(EPUB)
        self.assertEqual(b.bounds, [0, 1, 2, 4])  # front matter is chapter 0; c2b folds into chapter 2
        for chapter, offset in [(1, 0), (1, 250), (2, 50), (2, 700), (3, 400)]:
            xp, _ = b.moon_to_xpointer(chapter, offset)
            c, o, _ = b.xpointer_to_moon(xp)
            self.assertEqual(c, chapter, xp)
            self.assertLessEqual(abs(o - offset), len(PARA), xp)  # paragraph precision
        xp, frac = b.moon_to_xpointer(2, 700)  # past c2a's end -> lands in c2b
        self.assertTrue(xp.startswith('/body/DocFragment[4]/body'), xp)
        self.assertEqual(b.moon_to_xpointer(1, 205)[0], '/body/DocFragment[2]/body/p[3]')  # 'Ch 1' + 2 paras = 204

    def test_koreader_xpointer_forms(self):
        b = Book(EPUB)
        self.assertEqual(b.xpointer_to_moon('/body/DocFragment[2]/body/p[3]/text().10')[:2], (1, 4 + 200 + 10))
        self.assertEqual(b.xpointer_to_moon('/body/DocFragment[2]/body/h1/text().0')[:2], (1, 0))
        self.assertEqual(b.xpointer_to_moon('/body/DocFragment[2]/body/p[3]/autoBoxing/span')[:2], (1, 204))
        self.assertIsNone(b.xpointer_to_moon('12'))  # PDF page numbers aren't ours

    def test_partial_md5_small_file(self):
        with open(f'{TMP}/small', 'wb') as f:
            f.write(b'abc')
        self.assertEqual(partial_md5(f'{TMP}/small'), hashlib.md5(b'abc').hexdigest())


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(f'{TMP}/moon/{PO_NAME}', 'w') as f:
            f.write('1700000000000*2@0#105:40.0%')
        os.utime(f'{TMP}/moon/{PO_NAME}', (1, 1))
        server.LIB.scan()
        cls.httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.url = f'http://127.0.0.1:{cls.httpd.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def call(self, method, path, body=None, key=hashlib.md5(b'pw').hexdigest()):
        req = urllib.request.Request(self.url + path, method=method, data=json.dumps(body).encode() if body else None,
                                     headers={'x-auth-user': 'me', 'x-auth-key': key})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_flow(self):
        self.assertEqual(self.call('GET', '/users/auth')[0], 200)
        self.assertEqual(self.call('GET', '/users/auth', key='wrong')[0], 401)
        self.assertEqual(self.call('GET', '/syncs/progress/abc', key='wrong')[0], 401)
        self.assertEqual(self.call('POST', '/users/create', {'username': 'x', 'password': 'y'})[0], 402)

        # Moon+ position shows up for KOReader, matched by either document id method
        with server.closing(server.db()) as conn:
            server.import_moon_files(conn)
        for doc in (partial_md5(EPUB), hashlib.md5(PO_NAME[:-3].encode()).hexdigest()):
            code, body = self.call('GET', f'/syncs/progress/{doc}')
            self.assertEqual(code, 200)
            self.assertEqual(body['progress'], '/body/DocFragment[3]/body/p')  # "intro" paragraph
            self.assertAlmostEqual(body['percentage'], 0.40)

        # KOReader position is written into Moon+'s file, keeping its id prefix
        code, _ = self.call('PUT', '/syncs/progress', {'document': partial_md5(EPUB), 'device': 'kobo', 'device_id': 'K1',
                                                       'progress': '/body/DocFragment[5]/body/p[2]/text().3', 'percentage': 0.9})
        self.assertEqual(code, 200)
        self.assertEqual(open(f'{TMP}/moon/{PO_NAME}').read(), '1700000000000*3@0#107:90.0%')
        with server.closing(server.db()) as conn:
            server.import_moon_files(conn)  # our own write must not bounce back as a Moon+ update
        self.assertEqual(self.call('GET', f'/syncs/progress/{partial_md5(EPUB)}')[1]['device_id'], 'K1')

        # unknown documents still sync KOReader-to-KOReader; bad bodies are rejected
        self.call('PUT', '/syncs/progress', {'document': 'zzz', 'progress': '7', 'percentage': 0.1})
        self.assertEqual(self.call('GET', '/syncs/progress/zzz')[1]['progress'], '7')
        self.assertEqual(self.call('PUT', '/syncs/progress', {'document': 'zzz'})[0], 403)


if __name__ == '__main__':
    unittest.main()
