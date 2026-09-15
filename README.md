# servewise — a safe local preview server for built sites

`servewise` is a tiny, dependency-free local web server for previewing a built
static site (Hugo, Sphinx, Jekyll, MkDocs, plain HTML). It is `python -m
http.server` with the four things that bite you during a real preview session
fixed.

```bash
pip install https://github.com/abdelhaleemahmed/servewise/releases/download/v1.1.0/servewise-1.1.0-py3-none-any.whl
servewise                 # serve the built site, cache off, on the first free port
```

Or grab the **single file** and run it — it is pure standard library, no install
needed:

```bash
python server.py
```

## Why not just `python -m http.server`?

Four things `servewise` does that the stdlib server does not:

**1. It disables caching.** Every response carries
`Cache-Control: no-store, no-cache, must-revalidate, max-age=0`, so an edited
stylesheet or script takes effect on an ordinary reload. Without that, a cached
copy of an old asset is indistinguishable from a broken new one — an expensive
confusion when the asset is a diagram viewer or a theme override.

**2. It takes the first free port from 8000.** No collision, no "address already
in use", and — importantly — no two servers quietly sharing a port. The port it
settled on is printed.

**3. It says who it is.** Every instance answers `GET /__serve_info` on loopback
with its version, file path, file hash, the directory it serves, and its pid — so
one copy can ask another what it is.

**4. It checks its own version.** At startup (and on demand with `--check`), it
compares itself — by **version string and by content hash** — against the
canonical copy on disk and against every other running instance, and reports
anything that differs. A clean run stays quiet.

## The bug that made port safety non-negotiable

The obvious way to pick a port is `try: bind() except: next port`. On Windows
there is **no exception to catch**: `HTTPServer` sets `allow_reuse_address`, and
Windows honours `SO_REUSEADDR` by letting a second process bind a port that is
*already being listened on*. Both sockets hold the port, and which one answers a
given request is undefined.

That is not theoretical. Two copies of this server — one serving the hub, one
serving a book's build output — were both bound to 8000. A request returned `200`
with exactly the expected `no-store` header, and served **the wrong site**.
Nothing failed, nothing logged, and "the server is up" looked verified. Only
comparing the response *body* revealed it.

So `servewise` probes the port with an **outbound connection** before binding,
which behaves the same on every platform. (A socket in `TIME_WAIT` refuses the
connection, so restarting immediately after a shutdown still works.)

## Usage

```bash
servewise                 # first free port from 8000; serves the nearest built site
servewise 8010            # start the search at 8010 instead
servewise --strict 8010   # use exactly 8010, or fail (don't wander to another port)
servewise --root DIR      # serve some other directory
servewise --check         # audit versions across copies + running instances, then exit
servewise --no-scan       # skip the network scan of other instances at startup
```

`python -m servewise …` and the `servewise` command are equivalent; the single
file runs as `python server.py …`.

### Where it serves from

The root is found by **walking up from the script to the nearest directory that
contains an `index.html`** — not the working directory. So every copy behaves the
same with no arguments and no `cd`, and whatever it settles on is printed on
startup. `--root` overrides it.

> Tip (Windows): prefer `python path/to/build/serve.py` over
> `cd build && python serve.py`. A process whose working directory is inside
> `build/` prevents `rm -rf build`, so a clean rebuild fails while the server is
> up. `servewise` serves the same tree without that trap.

## Version checking, briefly

When a project keeps several copies of the server (one per built output, say),
they drift: a copy gets edited without a version bump, or a superseded copy
lingers. `servewise` catches both — the **version string** flags a superseded
copy, the **content hash** flags an edited one. Point `SERVE_CANON` at the
canonical file to make the check work from anywhere; a copy that cannot find the
canonical reports it as *not located* rather than pretending to have checked.

## Requirements

Python 3.8+ and nothing else — it is standard library only.

## License

MIT — see [LICENSE](LICENSE).
