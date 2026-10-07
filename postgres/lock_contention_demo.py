#!/usr/bin/env python3

"""
Live demonstration of README.md Difference 2 (lock queueing),
and of the standard mitigation.

Each scenario runs three concurrent connections, timed:
  A. Opens a transaction and SELECTs ... FOR UPDATE a row, holding the
     transaction open for a few seconds -- standing in for any in-flight
     write or long-running read a real microservice might have open.
  B. Shortly after, tries to run an ALTER TABLE ADD COLUMN. This needs an
     ACCESS EXCLUSIVE lock, which conflicts with A's open transaction.
  C. A reader loop issuing a plain SELECT every READ_INTERVAL seconds --
     the same kind of read microservice_one.py does. It only needs
     ACCESS SHARE, which is compatible with A's lock, but PostgreSQL's lock
     queue is FIFO/fair: while B's exclusive request is queued, C's reads
     queue behind it too, rather than jumping ahead.

Scenario 1 -- plain ALTER TABLE:
  B waits for A for as long as A stays open, and every read from C that
  arrives in the meantime stalls behind B. The ALTER itself is fast once it
  runs; the damage is the queue it creates.

Scenario 2 -- SET lock_timeout + retry (the standard mitigation):
  B gives up after LOCK_TIMEOUT instead of waiting indefinitely, backs off,
  and retries until A has finished. C's reads now wait at most about
  LOCK_TIMEOUT, and only when they arrive during a retry window.

The point for the comparison: the mitigation works, and it still runs as a
single script -- but someone has to know to write it. Adding fields in
MongoDB involves no DDL and no table lock, so there is nothing to mitigate.

While it runs, watch it live in a psql session with:
  SELECT pid, pg_blocking_pids(pid) AS blocked_by, state,
         wait_event_type, wait_event, left(query, 50) AS query
  FROM pg_stat_activity
  WHERE datname = current_database() AND pid <> pg_backend_pid();
"""

import threading
import time

import psycopg2
import psycopg2.errors

import demo_settings

HOLD_SECONDS = 8          # how long connection A keeps its transaction open
DDL_START = 1.5           # when connection B requests the ALTER
READ_INTERVAL = 0.5       # pause between connection C's reads
SLOW_READ = 0.1           # reads slower than this are logged individually
LOCK_TIMEOUT = "1s"       # scenario 2: how long the ALTER may wait per attempt
RETRY_BACKOFF = 1.0       # scenario 2: pause between ALTER attempts
MAX_ATTEMPTS = 20
DEMO_COLUMN = "demo_lock_col"

t0 = time.perf_counter()


def log(msg):
    print(f"[t+{time.perf_counter() - t0:6.2f}s] {msg}")


def get_connection():
    return psycopg2.connect(
        host=demo_settings.PG_HOST,
        port=demo_settings.PG_PORT,
        dbname=demo_settings.PG_DBNAME,
        user=demo_settings.PG_USER,
        password=demo_settings.PG_PASSWORD,
    )


def connection_a_long_running_transaction():
    conn = get_connection()
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute(f"SELECT emp_no FROM {demo_settings.TABLE_NAME} LIMIT 1;")
        emp_no = cur.fetchone()[0]
        log(f"Connection A: BEGIN; SELECT emp_no={emp_no} FOR UPDATE "
            f"(simulating an in-flight microservice transaction)")
        cur.execute(
            f"SELECT emp_no FROM {demo_settings.TABLE_NAME} WHERE emp_no = %s FOR UPDATE;",
            (emp_no,),
        )
        cur.fetchone()
    log(f"Connection A: holding transaction open for {HOLD_SECONDS}s ...")
    time.sleep(HOLD_SECONDS)
    conn.commit()
    log("Connection A: COMMIT (lock released)")
    conn.close()


def alter_sql():
    return (f"ALTER TABLE {demo_settings.TABLE_NAME} "
            f"ADD COLUMN IF NOT EXISTS {DEMO_COLUMN} INT;")


def connection_b_ddl():
    time.sleep(DDL_START)
    conn = get_connection()
    conn.autocommit = True
    log(f"Connection B: ALTER TABLE ADD COLUMN {DEMO_COLUMN} requested "
        f"-- will queue behind A's open transaction")
    start = time.perf_counter()
    with conn.cursor() as cur:
        cur.execute(alter_sql())
    waited = time.perf_counter() - start
    log(f"Connection B: ALTER TABLE completed -- waited {waited:.2f}s for the lock")
    conn.close()


def connection_b_ddl_with_lock_timeout():
    time.sleep(DDL_START)
    conn = get_connection()
    conn.autocommit = True
    start = time.perf_counter()
    with conn.cursor() as cur:
        cur.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}';")
        for attempt in range(1, MAX_ATTEMPTS + 1):
            log(f"Connection B: attempt {attempt}: SET lock_timeout = '{LOCK_TIMEOUT}'; "
                f"ALTER TABLE ADD COLUMN {DEMO_COLUMN}")
            try:
                cur.execute(alter_sql())
            except psycopg2.errors.LockNotAvailable:
                log(f"Connection B: attempt {attempt} gave up after {LOCK_TIMEOUT} "
                    f"(lock timeout) -- queued readers released; retrying in {RETRY_BACKOFF}s")
                time.sleep(RETRY_BACKOFF)
                continue
            log(f"Connection B: ALTER TABLE completed on attempt {attempt} "
                f"-- {time.perf_counter() - start:.2f}s after the first request")
            break
        else:
            log(f"Connection B: gave up after {MAX_ATTEMPTS} attempts -- "
                f"a real migration would fail here and be re-run later")
    conn.close()


def connection_c_reader_loop(stats):
    time.sleep(1.0)
    conn = get_connection()
    conn.autocommit = True
    log("Connection C: starting reader loop (plain SELECT, what microservice_one.py does)")
    stop_at = time.perf_counter() + HOLD_SECONDS + 0.5
    with conn.cursor() as cur:
        while time.perf_counter() < stop_at:
            start = time.perf_counter()
            cur.execute(f"SELECT count(*) FROM {demo_settings.TABLE_NAME};")
            cur.fetchone()
            waited = time.perf_counter() - start
            stats.append(waited)
            if waited > SLOW_READ:
                log(f"Connection C: SELECT took {waited:.2f}s -- stalled behind the pending ALTER")
            time.sleep(READ_INTERVAL)
    conn.close()


def cleanup():
    conn = get_connection()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"ALTER TABLE {demo_settings.TABLE_NAME} DROP COLUMN IF EXISTS {DEMO_COLUMN};")
    conn.close()


def run_scenario(title, ddl_target):
    global t0
    print("=" * 78)
    print(title)
    print("=" * 78)
    t0 = time.perf_counter()
    read_times = []
    threads = [
        threading.Thread(target=connection_a_long_running_transaction),
        threading.Thread(target=ddl_target),
        threading.Thread(target=connection_c_reader_loop, args=(read_times,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    cleanup()

    slow = [w for w in read_times if w > SLOW_READ]
    print(f"\n  Reader summary: {len(read_times)} reads, {len(slow)} stalled, "
          f"longest wait {max(read_times, default=0):.2f}s\n")
    return read_times


if __name__ == "__main__":
    print("Lock contention demo: run create_model.py first if you haven't.\n")
    print("While this runs, you can watch it live in a psql session with:")
    print("  SELECT pid, pg_blocking_pids(pid) AS blocked_by, state,")
    print("         wait_event_type, wait_event, left(query, 50) AS query")
    print("  FROM pg_stat_activity")
    print("  WHERE datname = current_database() AND pid <> pg_backend_pid();\n")

    cleanup()  # in case a previous run was interrupted

    plain = run_scenario(
        "Scenario 1: plain ALTER TABLE -- reads queue behind the pending DDL",
        connection_b_ddl,
    )
    mitigated = run_scenario(
        f"Scenario 2: SET lock_timeout = '{LOCK_TIMEOUT}' + retry -- the standard mitigation",
        connection_b_ddl_with_lock_timeout,
    )

    print("=" * 78)
    print(f"Longest read wait -- plain ALTER: {max(plain, default=0):.2f}s, "
          f"with lock_timeout: {max(mitigated, default=0):.2f}s")
    print("Same schema change, same open transaction. The difference is entirely")
    print("in how the migration was written.")
    print("\nDemo column dropped. Operation completed successfully!!!")
