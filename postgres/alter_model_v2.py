#!/usr/bin/env python3

"""
Change v2 on PostgreSQL: hobbies need structure.

After alter_model.py (v1), hobbies are a flat `text[]` array. Now each hobby
needs a level and a start year: {hobby, level, since}. A text[] can't hold
that, and the standard relational answer is a child table. (The other
option, a JSONB column, is out of scope here and covered separately.)

Moving a live system from the array to a child table is an expand-contract
migration. Some steps are DDL/data steps this script can run; others are
application deploys that a script can't do for you. This script runs the
database steps, times them, and prints the deploy steps in between:

  1. Expand: CREATE TABLE employee_hobbies + FK + index      (this script)
  2. Deploy app that writes BOTH the array and the new table  (app deploy)
  3. Copy existing hobbies from the array into the table      (this script)
  4. Deploy app that reads from the new table (JOIN)          (app deploy)
     -- in this demo: microservice_two_v2.py
  5. Contract: stop writing the array, then DROP COLUMN       (--contract)

Usage:
  ./alter_model_v2.py              # steps 1 and 3
  ./alter_model_v2.py --contract   # step 5, once nothing reads the array

Compare ../mongodb/alter_model_v2.py, the MongoDB version of the same change.
"""

import random
import sys
import time

import psycopg2
import psycopg2.errors

import demo_settings

BATCH_SIZE = 1000
LEVELS = ["casual", "intermediate", "advanced"]


def get_connection():
    return psycopg2.connect(
        host=demo_settings.PG_HOST,
        port=demo_settings.PG_PORT,
        dbname=demo_settings.PG_DBNAME,
        user=demo_settings.PG_USER,
        password=demo_settings.PG_PASSWORD,
    )


def timed(label, fn):
    start = time.perf_counter()
    result = fn()
    elapsed = time.perf_counter() - start
    print(f"  [{elapsed * 1000:8.2f} ms] {label}")
    return result


def column_exists(conn, table, column):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_name = %s AND column_name = %s;",
            (table, column),
        )
        return cur.fetchone() is not None


def table_exists(conn, table):
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL;", (table,))
        return cur.fetchone()[0]


def create_hobbies_table(conn):
    with conn.cursor() as cur:
        cur.execute(f"""
            CREATE TABLE {demo_settings.HOBBIES_TABLE_NAME} (
                id      BIGSERIAL PRIMARY KEY,
                emp_no  INTEGER NOT NULL REFERENCES {demo_settings.TABLE_NAME}(emp_no)
                            ON DELETE CASCADE,
                hobby   VARCHAR(50) NOT NULL,
                level   VARCHAR(20),
                since   SMALLINT
            );
        """)
        # Needed so the JOIN (and FK checks on employee deletes) can find an
        # employee's hobby rows. MongoDB has no equivalent: hobbies are embedded.
        cur.execute(f"""
            CREATE INDEX idx_{demo_settings.HOBBIES_TABLE_NAME}_emp_no
                ON {demo_settings.HOBBIES_TABLE_NAME} (emp_no);
        """)
    conn.commit()


def copy_hobbies_in_batches(conn):
    # On a large table this copy runs in batches so no single transaction
    # holds locks or generates WAL for too long. Every existing hobby must be
    # copied before reads can switch to the new table (step 4).
    with conn.cursor() as cur:
        cur.execute(f"SELECT min(emp_no), max(emp_no) FROM {demo_settings.TABLE_NAME};")
        lo, hi = cur.fetchone()
    copied = 0
    batches = 0
    for start in range(lo, hi + 1, BATCH_SIZE):
        with conn.cursor() as cur:
            cur.execute(
                f"""INSERT INTO {demo_settings.HOBBIES_TABLE_NAME} (emp_no, hobby)
                    SELECT emp_no, unnest(hobbies)
                    FROM {demo_settings.TABLE_NAME}
                    WHERE hobbies IS NOT NULL AND emp_no >= %s AND emp_no < %s;""",
                (start, start + BATCH_SIZE),
            )
            copied += cur.rowcount
        conn.commit()
        batches += 1
    return copied, batches


def add_structure_for_sample(conn):
    # Mirror the MongoDB version: about half of the employees with hobbies get
    # the new structured details (level, since); the rest have none yet.
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT DISTINCT emp_no FROM {demo_settings.HOBBIES_TABLE_NAME} "
            f"ORDER BY emp_no;"
        )
        emp_nos = [row[0] for row in cur.fetchall()]
        sample = random.sample(emp_nos, len(emp_nos) // 2)
        cur.execute(
            f"""UPDATE {demo_settings.HOBBIES_TABLE_NAME}
                SET level = (ARRAY{LEVELS}::text[])[1 + floor(random() * {len(LEVELS)})::int],
                    since = 2000 + floor(random() * 26)::int
                WHERE emp_no = ANY(%s);""",
            (sample,),
        )
    conn.commit()
    return len(sample)


def contract(conn):
    # Dropping a column is fast, but it needs an ACCESS EXCLUSIVE lock, so use
    # lock_timeout as in lock_contention_demo.py rather than risk a lock queue.
    with conn.cursor() as cur:
        cur.execute("SET lock_timeout = '2s';")
        cur.execute(f"ALTER TABLE {demo_settings.TABLE_NAME} DROP COLUMN hobbies;")
    conn.commit()


def print_step(n, text):
    print(f"\nStep {n}: {text}")


if __name__ == "__main__":
    conn = None
    try:
        conn = get_connection()
        print("Connected to PostgreSQL")

        if "--contract" in sys.argv:
            print_step(5, "contract -- DROP COLUMN employees.hobbies")
            if not column_exists(conn, demo_settings.TABLE_NAME, "hobbies"):
                print("  The hobbies array column is already gone. Nothing to do.")
                raise SystemExit(0)
            print("  Only safe once NO deployed code reads or writes the array. If\n"
                  "  microservice_two.py (v1) is still running, it will now fail.")
            try:
                timed("ALTER TABLE employees DROP COLUMN hobbies (lock_timeout 2s)",
                      lambda: contract(conn))
            except psycopg2.errors.LockNotAvailable:
                conn.rollback()
                print("  Lock timeout: another transaction is using the table. Try again later.")
                raise SystemExit(1)
            print("\nMigration complete: hobbies now live only in the child table.")
            raise SystemExit(0)

        if not column_exists(conn, demo_settings.TABLE_NAME, "hobbies"):
            print("ERROR: employees.hobbies doesn't exist. Run alter_model.py (v1) first.")
            raise SystemExit(1)
        if table_exists(conn, demo_settings.HOBBIES_TABLE_NAME):
            print(f"ERROR: {demo_settings.HOBBIES_TABLE_NAME} already exists -- v2 has already run.\n"
                  f"To start over: ./create_model.py && ./alter_model.py")
            raise SystemExit(1)

        print("\nChange v2: hobbies need structure ({hobby, level, since}).")
        print("A text[] array can't hold that, so move hobbies to a child table.")

        print_step(1, "expand -- create the child table (database)")
        timed(f"CREATE TABLE {demo_settings.HOBBIES_TABLE_NAME} + FK + index on emp_no",
              lambda: create_hobbies_table(conn))

        print_step(2, "APP DEPLOY -- not done by this script")
        print("  Deploy a version of every writer that saves hobbies to BOTH the array\n"
              "  and the new table, in one transaction. Without it, edits made during\n"
              "  the copy in step 3 would be lost from one side or the other.")

        print_step(3, "copy existing hobbies from the array into the table (database)")
        copied, batches = timed("INSERT ... SELECT emp_no, unnest(hobbies), in batches",
                                lambda: copy_hobbies_in_batches(conn))
        print(f"  Copied {copied} hobby rows in {batches} batches of up to {BATCH_SIZE} employees.")
        sampled = timed("UPDATE level/since for about half of the employees (new structured data)",
                        lambda: add_structure_for_sample(conn))
        print(f"  {sampled} employees now have structured hobby details.")

        print_step(4, "APP DEPLOY -- switch reads to the new table")
        print("  In this demo: stop microservice_two.py (reads the array) and start\n"
              "  microservice_two_v2.py (JOINs the child table). From now on, every\n"
              "  read of an employee's hobbies is a JOIN + aggregate.")

        print_step(5, "contract -- later, once nothing reads or writes the array")
        print("  Remove the dual-write code, deploy, then run: ./alter_model_v2.py --contract")

        print("\nOperation completed successfully!!!")

    except psycopg2.OperationalError as e:
        print("Could not connect to PostgreSQL:", e)
    finally:
        if conn:
            conn.close()
