"""EPUB structure and position conversion between Moon+ Reader (.po) and KOReader (xpointer)."""
import hashlib
import html
import posixpath
import re
import zipfile
from bisect import bisect_right
from html.parser import HTMLParser

VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'source', 'track', 'wbr'}


def partial_md5(path):
    """KOReader's default document id (util.partialMD5)."""
    m = hashlib.md5()
    with open(path, 'rb') as f:
        for i in range(-1, 11):
            # LuaJIT's bit.lshift(1024, -2) wraps to 0, so the first sample is at offset 0
            f.seek(0 if i < 0 else 1024 << (2 * i))
            chunk = f.read(1024)
            if not chunk:
                break
            m.update(chunk)
    return m.hexdigest()


def title_key(title):
    """Match key for a title: Moon+ strips punctuation from filenames, so compare letters and digits only."""
    return re.sub(r'[^a-z0-9]', '', html.unescape(title).lower())


def text_len(s):
    # shortcut: raw character count fits Moon+ percentages within ~1%; calibrate against paired real positions
    return len(s)


class _Doc(HTMLParser):
    """One spine file: total text length plus the text offset where each body element starts."""

    def __init__(self, data):
        super().__init__(convert_charrefs=True)
        self.length = 0
        self.starts = []   # [(offset, path)] in document order
        self.offset_of = {}
        self._stack = None  # [(path, {tag: count})], None until <body>
        self.feed(data)
        self.close()

    def handle_starttag(self, tag, attrs):
        tag = tag.lower().split(':')[-1]
        if self._stack is None:
            if tag == 'body':
                self._stack = [('', {})]
                self._record('')
            return
        parent, counts = self._stack[-1]
        counts[tag] = counts.get(tag, 0) + 1
        path = f'{parent}/{tag}[{counts[tag]}]'
        self._record(path)
        if tag not in VOID:
            self._stack.append((path, {}))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower().split(':')[-1] not in VOID and self._stack and len(self._stack) > 1:
            self._stack.pop()

    def handle_endtag(self, tag):
        if not self._stack:
            return
        tag = tag.lower().split(':')[-1]
        if tag == 'body':
            self._stack = []
            return
        # tolerate unclosed tags: pop back to the matching open element
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i][0].rsplit('/', 1)[-1].split('[')[0] == tag:
                del self._stack[i:]
                return

    def handle_data(self, data):
        if self._stack:
            self.length += text_len(data)

    def _record(self, path):
        self.starts.append((self.length, path))
        self.offset_of.setdefault(path, self.length)

    def locate(self, offset):
        """Path of the last element starting at or before offset."""
        i = bisect_right([s for s, _ in self.starts], offset) - 1
        return self.starts[max(i, 0)][1] if self.starts else ''


def crengine_path(path):
    # crengine writes p[1] as plain p; it parses both forms
    return re.sub(r'\[1\]', '', path)


class Book:
    def __init__(self, path):
        with zipfile.ZipFile(path) as z:
            opf_path = re.search(r'full-path="([^"]+)"', z.read('META-INF/container.xml').decode()).group(1)
            opf = z.read(opf_path).decode('utf-8', 'replace')
            base = posixpath.dirname(opf_path)
            items = {}
            for tag in re.findall(r'<item\b[^>]*>', opf):
                i, h = re.search(r'\bid="([^"]+)"', tag), re.search(r'\bhref="([^"]+)"', tag)
                if i and h:
                    items[i.group(1)] = posixpath.normpath(posixpath.join(base, html.unescape(h.group(1))))
            self.spine = [items[r] for r in re.findall(r'<itemref\b[^>]*\bidref="([^"]+)"', opf) if r in items]
            names = set(z.namelist())
            self.files = [_Doc(z.read(f).decode('utf-8', 'replace') if f in names else '') for f in self.spine]
            toc = self._toc(z, opf, items)
        t = re.search(r'<dc:title[^>]*>(.*?)</dc:title>', opf, re.S)
        self.title = html.unescape(t.group(1).strip()) if t else ''
        # Moon+ "chapter" = index of segments starting at spine 0 and at each spine file the TOC points to
        self.bounds = sorted({0} | {self.spine.index(f) for f in toc if f in self.spine})
        self.total = sum(d.length for d in self.files) or 1

    @staticmethod
    def _toc(z, opf, items):
        names = z.namelist()
        ncx = re.search(r'<spine\b[^>]*\btoc="([^"]+)"', opf)
        ncx_path = items.get(ncx.group(1)) if ncx else next((n for n in names if n.endswith('.ncx')), None)
        nav = re.search(r'<item\b(?=[^>]*properties="[^"]*\bnav\b)[^>]*\bhref="([^"]+)"', opf)
        nav_id = next((k for k, v in items.items() if nav and v.endswith(posixpath.basename(nav.group(1)))), None)
        for path, pattern in ((ncx_path, r'<content\b[^>]*\bsrc="([^"]+)"'), (items.get(nav_id), r'href="([^"]+)"')):
            if path and path in names:
                text = z.read(path).decode('utf-8', 'replace')
                if pattern.startswith('href'):
                    m = re.search(r'(?s)<nav\b[^>]*toc.*?</nav>', text)
                    text = m.group(0) if m else text
                d = posixpath.dirname(path)
                found = [posixpath.normpath(posixpath.join(d, html.unescape(s).split('#')[0])) for s in re.findall(pattern, text)]
                if found:
                    return found
        return []

    def _abs(self, j, offset):
        return sum(d.length for d in self.files[:j]) + offset

    def moon_to_xpointer(self, chapter, offset):
        """Moon+ (chapter, char offset) -> (KOReader xpointer, fraction)."""
        c = min(max(chapter, 0), len(self.bounds) - 1)
        j = self.bounds[c]
        # offsets run on past the file's end when the TOC skips files, and in some books past the next
        # TOC file too (Blood Over Bright Haven), so spill into following files regardless of chapter bounds
        while j + 1 < len(self.files) and offset > self.files[j].length:
            offset -= self.files[j].length
            j += 1
        offset = min(offset, self.files[j].length)
        xp = f'/body/DocFragment[{j + 1}]/body' + crengine_path(self.files[j].locate(offset))
        return xp, self._abs(j, offset) / self.total

    def xpointer_to_moon(self, xpointer):
        """KOReader xpointer -> (chapter, char offset, fraction), or None if it isn't one of ours."""
        m = re.match(r'/body/DocFragment\[(\d+)\](?:/body)?(.*)$', xpointer or '')
        if not m or not 0 < int(m.group(1)) <= len(self.files):
            return None
        j, rest = int(m.group(1)) - 1, m.group(2)
        t = re.search(r'/text\(\)(?:\[\d+\])?\.(\d+)$', rest)
        extra = int(t.group(1)) if t else 0
        rest = re.sub(r'/text\(\).*$', '', rest)
        rest = re.sub(r'\.\d+$', '', rest)  # xpointers to elements can also carry .N
        steps = [s if '[' in s else s + '[1]' for s in rest.split('/') if s]
        doc = self.files[j]
        # walk up until a known element: crengine may have nodes our parser doesn't (e.g. autoBoxing)
        while steps and '/' + '/'.join(steps) not in doc.offset_of:
            steps.pop()
            extra = 0
        within = min(doc.offset_of.get('/' + '/'.join(steps), 0) + extra, doc.length)
        c = bisect_right(self.bounds, j) - 1
        offset = self._abs(j, within) - self._abs(self.bounds[c], 0)
        return c, offset, self._abs(j, within) / self.total
