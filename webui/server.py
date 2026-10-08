#!/usr/bin/env python3

"""
Web UI for the MongoDB vs. PostgreSQL evolvability demo.

Replaces the multi-terminal walkthrough in ../README.md with one browser
page. Behind the scenes it still runs the same scripts in ../mongodb and
../postgres, unchanged, as subprocesses, and streams their output to the
page. It also:
  - checks both databases and shows what state the data is in,
  - shows the PostgreSQL lock queue live (instead of psql + \\watch),
  - shows sample documents and rows (instead of Compass / psql),
  - starts and stops the Postgres container with docker compose.

Only the scripts and arguments listed in SCRIPTS can be run, and the server
only listens on 127.0.0.1.

Usage (from the project directory):
  python webui/server.py                 # opens http://127.0.0.1:8765
  python webui/server.py --port 9000 --no-browser

Uses only the standard library plus pymongo and psycopg2 from
../requirements.txt.
"""

import argparse
import collections
import importlib.util
import itertools
import json
import os
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import psycopg2
import psycopg2.extras
import pymongo

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
DIRS = {"mongodb": ROOT / "mongodb", "postgres": ROOT / "postgres"}

# Connections made by this server identify themselves with this name, so the
# lock monitor can leave them out of the picture.
APP_NAME = "demo-webui"


def load_settings(db):
    # Both folders have a demo_settings.py, so load each under its own name.
    spec = importlib.util.spec_from_file_location(f"{db}_demo_settings", DIRS[db] / "demo_settings.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MONGO = load_settings("mongodb")
PG = load_settings("postgres")

# The only scripts the browser can run, with the arguments each one accepts.
SCRIPTS = {
    "mongodb": {
        "create_model.py": [],
        "microservice_one.py": [],
        "microservice_two.py": [],
        "alter_model.py": [],
        "alter_model_v2.py": ["--convert-all"],
        "clean_environment.py": [],
    },
    "postgres": {
        "create_model.py": [],
        "microservice_one.py": [],
        "microservice_two.py": [],
        "microservice_two_v2.py": [],
        "alter_model.py": [],
        "alter_model_v2.py": ["--contract"],
        "lock_contention_demo.py": [],
        "clean_environment.py": [],
    },
}
# Long-running services get their own output panel; everything else shares
# the per-database "scripts" panel and runs one at a time.
SERVICES = {"microservice_one.py", "microservice_two.py", "microservice_two_v2.py"}
DOCKER_ACTIONS = {"up": ["up", "-d"], "down": ["down", "-v"], "ps": ["ps"]}

settings = {"mongo_uri": os.environ.get("MONGODB_URI", "")}


# ---------------------------------------------------------------------------
# Event bus: every output line and process state change, in order. The page
# follows it with Server-Sent Events and can resume after a reconnect.
# ---------------------------------------------------------------------------

class Bus:
    def __init__(self, maxlen=20000):
        self.cond = threading.Condition()
        self.seq = 0
        self.events = collections.deque(maxlen=maxlen)

    def publish(self, **event):
        with self.cond:
            self.seq += 1
            event["seq"] = self.seq
            self.events.append(event)
            self.cond.notify_all()

    def since(self, seq, timeout):
        with self.cond:
            if self.seq <= seq:
                self.cond.wait(timeout)
            if not self.events:
                return []
            first = self.events[0]["seq"]
            start = max(0, seq + 1 - first)
            return list(itertools.islice(self.events, start, None))


bus = Bus()


def note(channel, text, cls="sys"):
    bus.publish(type="line", ch=channel, text=text, cls=cls)


# ---------------------------------------------------------------------------
# Processes
# ---------------------------------------------------------------------------

class Proc:
    def __init__(self, key, channel, argv, cwd, env):
        self.key = key
        self.channel = channel
        self.stopping = False
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        self.popen = subprocess.Popen(
            argv, cwd=cwd, env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=flags,
        )
        bus.publish(type="proc", key=key, ch=channel, state="running")
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.popen.stdout:
            bus.publish(type="line", ch=self.channel, text=line.rstrip("\r\n"))
        code = self.popen.wait()
        if self.stopping:
            note(self.channel, "[stopped]")
        else:
            # Neutral even on 0: the scripts print a connection failure and
            # still exit 0, so the output above is what tells the story.
            note(self.channel, f"[exited with code {code}]", "sys" if code == 0 else "err")
        bus.publish(type="proc", key=self.key, ch=self.channel, state="exited",
                    code=code, stopped=self.stopping)

    def running(self):
        return self.popen.poll() is None

    def stop(self):
        if not self.running():
            return
        self.stopping = True
        if os.name == "nt":
            self.popen.terminate()
        else:
            self.popen.send_signal(signal.SIGINT)
            try:
                self.popen.wait(3)
            except subprocess.TimeoutExpired:
                self.popen.kill()
        try:
            self.popen.wait(5)
        except subprocess.TimeoutExpired:
            pass


class Conflict(Exception):
    pass


procs = {}
procs_lock = threading.Lock()


def running(key):
    p = procs.get(key)
    return bool(p and p.running())


def _launch(key, channel, argv, cwd, env, header):
    with procs_lock:
        for p in procs.values():
            if p.channel == channel and p.running():
                raise Conflict(f"{p.key} is still running in this panel. Wait for it or stop it first.")
        note(channel, header, "cmd")
        try:
            procs[key] = Proc(key, channel, argv, cwd, env)
        except OSError as e:
            note(channel, f"Could not start: {e}", "err")
            raise Conflict(f"Could not start {argv[0]}: {e}")


def run_script(db, script, args):
    if db not in SCRIPTS or script not in SCRIPTS[db]:
        raise ValueError(f"Unknown script: {db}/{script}")
    if any(a not in SCRIPTS[db][script] for a in args):
        raise ValueError(f"Unsupported arguments for {script}: {args}")

    env = os.environ.copy()
    env.update(PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    if db == "mongodb":
        if settings["mongo_uri"]:
            env["MONGODB_URI"] = settings["mongo_uri"]
        else:
            env.pop("MONGODB_URI", None)

    channel = f"{db}:{script[:-3]}" if script in SERVICES else f"{db}:scripts"
    argv = [sys.executable, "-u", script, *args]
    header = "$ " + " ".join([f"{db}/{script}", *args])
    _launch(f"{db}/{script}", channel, argv, DIRS[db], env, header)


def run_docker(action):
    if action not in DOCKER_ACTIONS:
        raise ValueError(f"Unknown docker action: {action}")
    argv = ["docker", "compose", *DOCKER_ACTIONS[action]]
    _launch("system/docker", "system", argv, ROOT, os.environ.copy(), "$ " + " ".join(argv))


def stop(key):
    p = procs.get(key)
    if p:
        p.stop()


def stop_all():
    for p in list(procs.values()):
        p.stop()


def wait_for(key, timeout=120):
    deadline = time.time() + timeout
    while running(key) and time.time() < deadline:
        time.sleep(0.2)


def reset(with_docker):
    # Stop everything, drop the demo data in both databases, and optionally
    # remove the Postgres container and its volume too.
    stop_all()
    for db in ("mongodb", "postgres"):
        try:
            run_script(db, "clean_environment.py", [])
        except Conflict as e:
            note(f"{db}:scripts", str(e), "err")
    if with_docker:
        wait_for("postgres/clean_environment.py")
        try:
            run_docker("down")
        except Conflict as e:
            note("system", str(e), "err")


# ---------------------------------------------------------------------------
# Database checks: what state is the demo data in?
# ---------------------------------------------------------------------------

_mongo = {"uri": None, "client": None}
_mongo_lock = threading.Lock()


def mongo_collection():
    uri = settings["mongo_uri"]
    if not uri:
        raise RuntimeError("MONGODB_URI is not set")
    with _mongo_lock:
        if _mongo["uri"] != uri:
            if _mongo["client"]:
                _mongo["client"].close()
            _mongo["client"] = pymongo.MongoClient(uri, serverSelectionTimeoutMS=4000, appname=APP_NAME)
            _mongo["uri"] = uri
        return _mongo["client"][MONGO.DB_NAME][MONGO.COLLECTION_NAME]


def mongo_host():
    uri = settings["mongo_uri"]
    if not uri:
        return None
    return uri.split("@")[-1].split("/")[0].split("?")[0]


def mongo_status():
    out = {"uri_set": bool(settings["mongo_uri"]), "host": mongo_host(), "ok": False}
    try:
        coll = mongo_collection()
        out["count"] = coll.count_documents({})
        out["with_department"] = coll.count_documents({"department": {"$type": "string"}})
        out["hobbies_strings"] = coll.count_documents({"hobbies": {"$type": "string"}})
        out["hobbies_objects"] = coll.count_documents({"hobbies": {"$elemMatch": {"name": {"$exists": True}}}})
        out["ok"] = True
    except Exception as e:
        out["err"] = str(e).split(", Timeout:")[0][:300]
    return out


class PgConn:
    """One autocommit connection per use, reconnected on failure. Short
    timeouts so the UI never waits in the lock queue it's showing."""

    def __init__(self):
        self.conn = None
        self.lock = threading.Lock()

    def query(self, sql, params=None, dict_rows=False):
        with self.lock:
            if self.conn is None or self.conn.closed:
                self.conn = psycopg2.connect(
                    host=PG.PG_HOST, port=PG.PG_PORT, dbname=PG.PG_DBNAME,
                    user=PG.PG_USER, password=PG.PG_PASSWORD,
                    connect_timeout=3, application_name=APP_NAME,
                    options="-c lock_timeout=300 -c statement_timeout=5000",
                )
                self.conn.autocommit = True
            factory = psycopg2.extras.RealDictCursor if dict_rows else None
            try:
                with self.conn.cursor(cursor_factory=factory) as cur:
                    cur.execute(sql, params)
                    return cur.fetchall()
            except psycopg2.OperationalError:
                self.conn.close()
                raise


pg_status_conn = PgConn()
pg_lock_conn = PgConn()


def pg_columns(table):
    rows = pg_status_conn.query(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = %s ORDER BY ordinal_position",
        (table,))
    return [{"name": r[0], "type": r[1]} for r in rows]


def pg_status():
    out = {"host": f"{PG.PG_HOST}:{PG.PG_PORT}/{PG.PG_DBNAME}", "ok": False}
    try:
        # Catalog queries only take no lock on the tables themselves.
        cols = [c["name"] for c in pg_columns(PG.TABLE_NAME)]
        out["table"] = bool(cols)
        out["department_column"] = "department" in cols
        out["hobbies_column"] = "hobbies" in cols
        out["hobbies_table"] = bool(pg_columns(PG.HOBBIES_TABLE_NAME))
        out["ok"] = True
        # Counting rows does take a lock. While the lock demo runs, skip it so
        # the UI doesn't join the queue the demo is showing.
        if running("postgres/lock_contention_demo.py"):
            out["busy"] = "lock demo running"
        elif out["table"]:
            try:
                dept = "count(department)" if out["department_column"] else "0"
                count, with_dept = pg_status_conn.query(
                    f"SELECT count(*), {dept} FROM {PG.TABLE_NAME}")[0]
                out["count"], out["with_department"] = count, with_dept
                if out["hobbies_table"]:
                    out["hobby_rows"] = pg_status_conn.query(
                        f"SELECT count(*) FROM {PG.HOBBIES_TABLE_NAME}")[0][0]
            except psycopg2.errors.LockNotAvailable:
                out["busy"] = "table locked"
            except psycopg2.errors.QueryCanceled:
                out["busy"] = "query timed out"
    except Exception as e:
        # libpq repeats the error for each address it tried; one line is enough.
        out["err"] = str(e).strip().splitlines()[0][:300]
    return out


_status_cache = {"at": 0, "value": None}
_status_lock = threading.Lock()


def status():
    with _status_lock:
        if time.time() - _status_cache["at"] > 1.5:
            _status_cache["value"] = {"mongodb": mongo_status(), "postgres": pg_status()}
            _status_cache["at"] = time.time()
        return _status_cache["value"]


def proc_state():
    return {k: {"running": p.running(), "code": p.popen.returncode,
                "ch": p.channel, "stopped": p.stopping}
            for k, p in procs.items()}


def locks():
    rows = pg_lock_conn.query(
        """
        SELECT pid, pg_blocking_pids(pid) AS blocked_by, state,
               wait_event_type, wait_event,
               round(extract(epoch FROM now() - query_start)::numeric, 1) AS seconds,
               left(regexp_replace(query, '\\s+', ' ', 'g'), 70) AS query
        FROM pg_stat_activity
        WHERE datname = current_database()
          AND pid <> pg_backend_pid()
          AND application_name <> %s
        ORDER BY pid
        """, (APP_NAME,), dict_rows=True)
    return rows


def peek(db):
    # A few sample records in each shape the data can be in right now.
    if db == "mongodb":
        coll = mongo_collection()
        queries = [
            ("Not yet given the v1 fields", {"department": {"$exists": False}}),
            ("v1: hobbies as plain strings", {"hobbies": {"$type": "string"}}),
            ("v2: hobbies as objects", {"hobbies": {"$elemMatch": {"name": {"$exists": True}}}}),
        ]
        samples = []
        for label, q in queries:
            doc = coll.find_one(q, {"_id": 0})
            if doc:
                samples.append({"label": label, "doc": doc})
        return {"samples": samples}

    st = pg_status()
    if not st.get("table"):
        return {"columns": [], "samples": []}
    out = {"columns": pg_columns(PG.TABLE_NAME), "samples": []}
    queries = [("Not yet given the v1 columns (NULLs)", "department IS NULL")]
    if st.get("department_column"):
        queries = [("Not yet given the v1 columns (NULLs)", "department IS NULL"),
                   ("Given the v1 columns", "department IS NOT NULL")]
    for label, where in queries:
        rows = pg_status_conn.query(
            f"SELECT * FROM {PG.TABLE_NAME} WHERE {where} ORDER BY emp_no LIMIT 2", dict_rows=True)
        if rows:
            out["samples"].append({"label": label, "rows": rows})
    if st.get("hobbies_table"):
        out["child_columns"] = pg_columns(PG.HOBBIES_TABLE_NAME)
        out["child_rows"] = pg_status_conn.query(
            f"SELECT * FROM {PG.HOBBIES_TABLE_NAME} WHERE emp_no = "
            f"(SELECT min(emp_no) FROM {PG.HOBBIES_TABLE_NAME}) ORDER BY 2", dict_rows=True)
    return out


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
}


class Handler(BaseHTTPRequestHandler):
    server_version = "DemoWebUI"

    def log_message(self, fmt, *args):
        pass

    def _host_ok(self):
        # Refuse requests addressed to any other host name (DNS rebinding).
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if host in ("127.0.0.1", "localhost"):
            return True
        self.send_error(403)
        return False

    def _json(self, value, code=200):
        body = json.dumps(value, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._host_ok():
            return
        url = urlparse(self.path)
        if url.path in STATIC_FILES:
            name, ctype = STATIC_FILES[url.path]
            body = (STATIC / name).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/api/events":
            self._events(url)
        elif url.path == "/api/state":
            self._json({"procs": proc_state(), "status": status()})
        elif url.path == "/api/locks":
            try:
                self._json({"rows": locks()})
            except Exception as e:
                self._json({"error": str(e).strip()[:300]})
        elif url.path == "/api/peek":
            db = parse_qs(url.query).get("db", [""])[0]
            try:
                self._json(peek(db))
            except Exception as e:
                self._json({"error": str(e).strip()[:300]})
        else:
            self.send_error(404)

    def _events(self, url):
        try:
            last = int(self.headers.get("Last-Event-ID") or parse_qs(url.query).get("since", ["0"])[0])
        except ValueError:
            last = 0
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            if last > bus.seq:
                # The server restarted since the page last saw it: start over.
                self.wfile.write(b"event: reset\ndata: {}\n\n")
                last = 0
            while True:
                events = bus.since(last, timeout=15)
                if not events:
                    self.wfile.write(b": keepalive\n\n")
                for e in events:
                    self.wfile.write(f"id: {e['seq']}\ndata: {json.dumps(e)}\n\n".encode())
                    last = e["seq"]
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return

    def do_POST(self):
        if not self._host_ok():
            return
        # JSON only: a cross-site form post can't send this content type
        # without a CORS preflight, which this server never answers.
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            self.send_error(415)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            path = urlparse(self.path).path
            if path == "/api/run":
                run_script(body.get("db"), body.get("script"), body.get("args") or [])
            elif path == "/api/stop":
                stop(body.get("key"))
            elif path == "/api/stop-all":
                stop_all()
            elif path == "/api/docker":
                run_docker(body.get("action"))
            elif path == "/api/reset":
                threading.Thread(target=reset, args=(bool(body.get("docker")),), daemon=True).start()
            elif path == "/api/mongo-uri":
                settings["mongo_uri"] = (body.get("uri") or "").strip()
                _status_cache["at"] = 0
            else:
                self.send_error(404)
                return
            self._json({"ok": True, "procs": proc_state()})
        except (Conflict, ValueError) as e:
            self._json({"ok": False, "error": str(e)}, 409)


def main():
    parser = argparse.ArgumentParser(description="Web UI for the MongoDB vs. PostgreSQL demo")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Demo web UI running at {url}  (Ctrl+C to stop; running scripts are stopped too)")
    if not settings["mongo_uri"]:
        print("MONGODB_URI is not set: you can enter it in the page instead.")
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, (url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("Stopping running scripts ...")
        stop_all()
        server.server_close()


if __name__ == "__main__":
    main()
