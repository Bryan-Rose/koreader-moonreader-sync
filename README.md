# koreader-moonreader-sync

Syncs reading progress between [KOReader](https://koreader.rocks) and Moon+ Reader Pro (Android).

- KOReader talks to this server with its built-in **Progress sync** plugin (the kosync protocol).
- Moon+ keeps syncing through WebDAV as usual. This server reads and writes Moon+'s `.po` position files in that WebDAV folder, so Moon+ needs no changes.
- Positions are translated between the two apps using the EPUB files themselves, to within about a paragraph.

Requirements:
- A WebDAV server that Moon+ already syncs to.
- A folder of the same EPUB files both readers use, for example the library behind your OPDS server.
- Python 3 standard library only; no dependencies.

## Deploy

```sh
cp .env.example .env    # then edit it
mkdir data              # must be writable by PUID/PGID (default 1000)
docker compose up -d --build
curl localhost:8081/healthcheck  # {"state": "OK"}
```

## KOReader setup

1. Open a book → Tools → Progress sync → Custom sync server: `http://<your-server>:8081`
2. Log in with `SYNC_USER` / `SYNC_PASSWORD`. "Register" also works with the same credentials.
3. Document matching method:
   - **Binary** (default): works when KOReader's copy is byte-identical to the file in `BOOKS_DIR`, for example when both readers download from the same OPDS server.
   - **Filename**: only works if KOReader's filename matches Moon+'s `Title - Author.epub`.
4. Turn on automatic sync if you like.

## How it works

- **Moon+ position file:** `.Moon+/Cache/<book file>.po`, which contains `<id>*<chapter>@<n>#<char offset>:<percent>%`.
- **Moon+ "chapter":** an index into sections of the book. A new section starts at the first file and at every file the table of contents points to.
- **Books** are matched to the library by EPUB title. Moon+ strips punctuation from its filenames, so only letters and digits are compared.
- **Moon+ changes** are picked up within `POLL_SECONDS` (10 s). KOReader receives them as an xpointer on its next pull.
- **A KOReader push** rewrites the book's `.po` file, and Moon+ picks it up the next time it syncs that book.
- **Conflicts:** the newest position wins.

## Known limits

- EPUB only. PDFs, and books Moon+ has never opened (no `.po` file yet), sync only between KOReader devices.
- If a reader has a different edition from the one in `BOOKS_DIR`, positions will be slightly off.
- Moon+'s `.po` format is undocumented. It was worked out from real files and tested live, not taken from a spec.

MIT licensed. Not affiliated with KOReader or Moon+ Reader.

## Tests

`python3 -m unittest test_sync`
