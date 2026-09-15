"""Local dev server with caching disabled, for previewing built docs.

    python scripts/serve.py            # first free port from 8000, serves the hub
    python scripts/serve.py 8010       # start looking from 8010 instead
    python scripts/serve.py --check    # audit versions, print a report, exit
    python scripts/serve.py --root DIR # serve some other directory
    python scripts/serve.py --strict   # use exactly the port named, or fail

CANONICAL COPY: scripts/serve.py. Every other copy in the tree is a duplicate of
this file and is expected to be byte-identical to it. The script checks that
itself -- see "Version checking" below -- so a stale copy announces itself
instead of waiting to be noticed.

Four things it does that `python -m http.server` does not.

**It disables caching.** Every response carries `Cache-Control: no-store`, so an
edited stylesheet or JavaScript file takes effect on an ordinary reload. Without
that, a cached copy of an old asset is indistinguishable from a broken new one --
an expensive confusion when the asset is a diagram viewer or a theme override.

**It takes the first free port from 8000.** No collision, no "address already in
use", no two servers quietly sharing a port. The port it settled on is printed.

**It says who it is.** Every instance answers `GET /__serve_info` on loopback
with its version, its own file path, that file's hash, the directory it serves
and its pid. That is how one copy can ask another what it is.

**It checks its own version.** At startup, against the canonical file on disk and
against every other instance it can find, reporting anything that differs.

Why the port check is not a `try`/`except` around `bind()`
-----------------------------------------------------------

Because on Windows there is no exception to catch. `HTTPServer` sets
`allow_reuse_address = 1`, and Windows honours `SO_REUSEADDR` by letting a second
process bind a port that is *already being listened on*. Both sockets then hold
the port and which one answers a given request is undefined.

That is not theoretical. Two copies of this script, one serving the hub and one
serving a book's build output, were both bound to 8000. A request returned 200
with exactly the expected `no-store` header -- and served the wrong book. Nothing
failed, nothing logged, and "the server is up" looked verified. Only comparing the
response *body* revealed it.

So the port is probed with an outbound connection before binding, which behaves
the same on every platform. A socket in `TIME_WAIT` refuses a connection, so
restarting immediately after a shutdown still works.

Version checking
----------------

Two comparisons, because either alone has a hole:

* **Version string** catches a deliberately superseded copy.
* **Content hash** catches an edited copy whose version was not bumped -- which is
  the more common way duplicates drift, since nothing forces a bump.

A mismatch of either is reported. The canonical file is found by looking at
`$SERVE_CANON` first, then by walking up from this file for `scripts/serve.py`.
A copy living outside the repo -- inside a book's build output, say -- cannot walk
up to it; that copy reports the canonical as *not located* rather than pretending
to have checked. Set `SERVE_CANON` to the canonical path to make the check work
from anywhere.

Running instances are found by scanning ports from 8000 and asking each one for
`/__serve_info`. An instance that answers HTTP but not that endpoint is either
something else entirely or a copy older than this mechanism; both are reported as
unknown, which is itself the signal to look.

Where to run it
---------------

The root is found by walking up from this file's location to the nearest
directory containing an `index.html` -- not the working directory. Every copy
then behaves the same with no arguments and no `cd`:

    python scripts/serve.py                # serves the hub (walks up one)
    python serve.py                        # at the repo root: serves the hub
    python docs/build/html/serve.py        # from a book's docs/: serves that book

Whatever it settles on is printed on startup, and `--root` overrides it.

Prefer `python docs/build/html/serve.py` over
`cd docs/build/html && python serve.py`. Both serve the same tree, but on Windows
a process whose working directory is inside `build/` prevents `rm -rf build`, so
`make clean` fails while the server is up.
"""
import concurrent.futures
import hashlib
import html
import json
import os
import re
import socket
import sys
import urllib.error
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

__version__ = "1.1.0"

HOST = "127.0.0.1"
BASE_PORT = 8000        # where the search for a free port starts
SCAN = 30               # how many ports to consider, for both search and audit
INFO_PATH = "/__serve_info"

SELF = os.path.abspath(__file__)


# -- identity ----------------------------------------------------------------

def file_hash(path):
    """sha256 of a file, or None. Short form is enough to compare by eye."""
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def file_version(path):
    """The `__version__` declared in a Python file, without importing it."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']',
                          fh.read(), re.M)
        return m.group(1) if m else None
    except OSError:
        return None


def self_info(root=None):
    return {
        "tool": "serve.py",
        "version": __version__,
        "sha256": file_hash(SELF),
        "path": SELF,
        "root": root,
        "pid": os.getpid(),
    }


def find_canonical(start=None):
    """The canonical scripts/serve.py, or None if this copy cannot see it."""
    override = os.environ.get("SERVE_CANON")
    if override:
        return override if os.path.isfile(override) else None

    here = os.path.dirname(start or SELF)
    for _ in range(6):
        candidate = os.path.join(here, "scripts", "serve.py")
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    return None


# -- talking to other instances ----------------------------------------------

def port_in_use(port, host=HOST, timeout=0.3):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def ask(port, host=HOST, timeout=0.8):
    """Ask whatever is on `port` who it is.

    Returns a dict from /__serve_info, or {"unknown": ...} for something that
    answers HTTP but not that endpoint, or None if nothing is listening.
    """
    if not port_in_use(port, host):
        return None
    try:
        with urllib.request.urlopen(f"http://{host}:{port}{INFO_PATH}",
                                    timeout=timeout) as r:
            data = json.loads(r.read(4096).decode("utf-8", "replace"))
        if isinstance(data, dict) and data.get("tool") == "serve.py":
            data["port"] = port
            return data
    except Exception:
        pass
    return {"port": port, "unknown": describe_port(port, host)}


def describe_port(port, host=HOST, timeout=0.8):
    """What is holding `port`, for something that is not one of ours."""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/", timeout=timeout) as r:
            head = r.read(8192).decode("utf-8", "replace")
            server = r.headers.get("Server", "")
    except urllib.error.HTTPError as exc:
        return f"an HTTP server (answered {exc.code} at /)"
    except Exception:
        return "listening, but not answering HTTP"

    m = re.search(r"<title>(.*?)</title>", head, re.S | re.I)
    if m:
        # Unescape, because a raw `&mdash;` in a diagnostic reads as a bug in the
        # diagnostic. Keep the part before the separator -- Sphinx renders
        # `<page> - <project>`. Then drop non-ASCII: an em dash arrived as a
        # replacement character on a cp1252 console, and a diagnostic that looks
        # broken undermines itself.
        title = " ".join(html.unescape(m.group(1)).split())
        title = re.split(r"\s+[—–-]\s+", title)[0]
        title = title.encode("ascii", "ignore").decode("ascii").strip()
        if len(title) > 48:
            title = title[:48].rsplit(" ", 1)[0] + "..."
        if title:
            return f'"{title}"'
    return f"an HTTP server ({server})" if server else "an HTTP server"


def scan(base=BASE_PORT, count=SCAN, host=HOST):
    """Every instance found on ports [base, base+count). Concurrent: one pass
    over 30 ports costs about as long as the slowest single probe."""
    found = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        futures = {pool.submit(ask, p, host): p
                   for p in range(base, base + count)}
        for fut in concurrent.futures.as_completed(futures):
            info = fut.result()
            if info:
                found.append(info)
    return sorted(found, key=lambda i: i["port"])


# -- the audit ---------------------------------------------------------------

def audit(base=BASE_PORT, count=SCAN, quiet=False):
    """Compare this copy with the canonical file and with every running
    instance. Returns (lines, differing_paths)."""
    lines = []
    differing = []

    canon = find_canonical()
    lines.append(f"this copy      {__version__}  {file_hash(SELF)[:12]}  {SELF}")

    if canon is None:
        lines.append("canonical      NOT LOCATED -- no scripts/serve.py above "
                     "this file.")
        lines.append("               Set SERVE_CANON to check a copy that lives "
                     "outside the repo.")
    else:
        cv, ch = file_version(canon), file_hash(canon)
        same = (cv == __version__) and (ch == file_hash(SELF))
        state = "matches" if same else "DIFFERS from this copy"
        lines.append(f"canonical      {cv or '?':6} {(ch or '?')[:12]}  {canon}")
        lines.append(f"               {state}")
        if not same:
            differing.append(SELF)

    instances = [i for i in scan(base, count) if i.get("pid") != os.getpid()]
    if not instances:
        lines.append(f"running        none found on ports {base}-{base + count - 1}")
        return lines, differing, 0

    lines.append(f"running        {len(instances)} on ports "
                 f"{base}-{base + count - 1}")
    for i in instances:
        if "unknown" in i:
            lines.append(f"  :{i['port']:<5} ?      {'?':12}  {i['unknown']}")
            continue
        mine = (i.get("version") == __version__
                and i.get("sha256") == file_hash(SELF))
        mark = "" if mine else "   <-- DIFFERENT"
        lines.append(f"  :{i['port']:<5} {i.get('version','?'):6} "
                     f"{(i.get('sha256') or '?')[:12]}  {i.get('path')}{mark}")
        if not mine:
            differing.append(i.get("path"))

    unknown = sum(1 for i in instances if "unknown" in i)
    return lines, differing, unknown


def print_audit(base=BASE_PORT, count=SCAN):
    lines, differing, unknown = audit(base, count)
    print("serve.py version audit")
    for line in lines:
        print("  " + line)
    if differing:
        print("\n  These differ from the canonical copy. Refresh each with:")
        canon = find_canonical() or "scripts/serve.py"
        for path in dict.fromkeys(differing):
            print(f"    copy {canon} -> {path}")
    else:
        print()
        print("  Every copy that identified itself is consistent.")

    if unknown:
        # Not counted as a failure: something else entirely may legitimately
        # be on one of these ports. But it cannot be called consistent
        # either -- a serve.py older than this mechanism looks like this.
        print(f"  {unknown} instance(s) did not answer {INFO_PATH}: either "
              f"not serve.py, or a copy older than {__version__}.")
        print("  Worth a look before assuming all is well.")
    return 1 if differing else 0


# -- the server --------------------------------------------------------------

def choose_port(port, strict=False, scan_count=SCAN):
    """The first free port at or after `port`, or None."""
    if not port_in_use(port):
        return port

    print(f"port {port} is in use by {describe_port(port)}", file=sys.stderr)
    if strict:
        print("  --strict given, so not moving. Free the port, or name another "
              "one.", file=sys.stderr)
        return None

    for candidate in range(port + 1, port + scan_count):
        if not port_in_use(candidate):
            print(f"  using port {candidate} instead", file=sys.stderr)
            return candidate

    print(f"  no free port between {port + 1} and {port + scan_count - 1}",
          file=sys.stderr)
    return None


def find_root(start, levels=3):
    """The nearest directory at or above `start` holding an index.html.

    The script lives in more than one place: scripts/, the repo root, and inside
    a book build output. "Serve the directory I am in" is right for two of those
    and would make the scripts/ copy serve a directory of shell scripts.

    Bounded, and the directory chosen is printed on startup, so the result is
    never a guess the user cannot see. --root overrides it entirely.
    """
    here = os.path.abspath(start)
    for _ in range(levels + 1):
        if os.path.isfile(os.path.join(here, "index.html")):
            return here
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    return os.path.abspath(start)


def make_handler(root):
    class NoCacheHandler(SimpleHTTPRequestHandler):
        def end_headers(self):
            self.send_header("Cache-Control",
                             "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            super().end_headers()

        def do_GET(self):
            if self.path.split("?", 1)[0] == INFO_PATH:
                return self.send_info()
            return super().do_GET()

        def send_info(self):
            # Loopback only. The payload contains filesystem paths and a pid,
            # and the server binds every interface, so this must not be
            # answerable from the network.
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                self.send_error(403, "info is loopback-only")
                return
            body = json.dumps(self_info(root), indent=1).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send_head(self):
            # `worktrees/` holds per-book git worktrees (dev checkouts), which
            # live inside the repo but are NOT part of the published hub. Never
            # serve them, so each book appears once — under its integrated
            # `books/<slug>/` path, not also under `worktrees/<slug>/`.
            path = self.path.split("?", 1)[0].split("#", 1)[0]
            if path == "/worktrees" or path.startswith("/worktrees/"):
                self.send_error(404, "Not serving worktrees (dev checkouts)")
                return None
            return super().send_head()

    return lambda *a, **k: NoCacheHandler(*a, directory=root, **k)


def main():
    argv = sys.argv[1:]
    strict = "--strict" in argv
    no_scan = "--no-scan" in argv
    check_only = "--check" in argv

    root = None
    if "--root" in argv:
        i = argv.index("--root")
        root = os.path.abspath(argv[i + 1])
        del argv[i:i + 2]
    positional = [a for a in argv if not a.startswith("-")]
    port = int(positional[0]) if positional else BASE_PORT

    if check_only:
        return print_audit(port)

    if root is None:
        root = find_root(os.path.dirname(SELF))
    if not os.path.isfile(os.path.join(root, "index.html")):
        print(f"warning: no index.html in {root}", file=sys.stderr)
        print("         build the docs first, or pass --root", file=sys.stderr)

    # Version check before serving. Cheap locally; the scan is concurrent and
    # skippable. Only anomalies are printed -- a clean run stays quiet.
    lines, differing, _unknown = audit(BASE_PORT, 0 if no_scan else SCAN)
    if differing:
        print("serve.py version mismatch", file=sys.stderr)
        for line in lines:
            print("  " + line, file=sys.stderr)
        canon = find_canonical() or "scripts/serve.py"
        print("  refresh with:", file=sys.stderr)
        for path in dict.fromkeys(differing):
            print(f"    copy {canon} -> {path}", file=sys.stderr)
        print("  (--check for the full report)\n", file=sys.stderr)

    chosen = choose_port(port, strict=strict)
    if chosen is None:
        return 2

    with ThreadingHTTPServer(("", chosen), make_handler(root)) as srv:
        print(f"serve.py {__version__}   {SELF}")
        print(f"Serving {root}")
        print(f"  http://{HOST}:{chosen}/    (cache disabled)")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
