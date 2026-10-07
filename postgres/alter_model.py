#!/usr/bin/env python3

"""
Applies the same logical schema change as ../mongodb/alter_model.py, but on
PostgreSQL (change v1: department, title, birth_date and a flat list of
hobbies).

This is deliberately the strongest relational design for this change:
  - adding plain columns is metadata-only and fast (PG 11+), and each DDL
    step is timed to show it -- this script does NOT claim "RDBMS schema
    changes cause downtime";
  - `hobbies` is a flat list of strings, so it's a native `text[]` array
    column: one atomic row write, no child table, no join.

For v1 the two databases are roughly a draw. The interesting change is v2
(alter_model_v2.py), when hobbies need structure. Lock behaviour while DDL
runs is shown separately in lock_contention_demo.py.
"""

import datetime
import random
import time

import psycopg2
from psycopg2.extras import execute_values

import demo_settings


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
    fn()
    elapsed = time.perf_counter() - start
    print(f"  [{elapsed * 1000:8.2f} ms] {label}")


def add_columns(conn):
    with conn.cursor() as cur:
        cur.execute(f"""
            ALTER TABLE {demo_settings.TABLE_NAME}
                ADD COLUMN birth_date DATE,
                ADD COLUMN department VARCHAR(50),
                ADD COLUMN title      VARCHAR(50),
                ADD COLUMN hobbies    TEXT[];
        """)
    conn.commit()


def add_department_index(conn):
    # microservice_two.py filters on department; without this index that
    # query degenerates to a sequential scan once the table is populated.
    # ../mongodb/alter_model.py creates the same index in MongoDB, so this
    # step is like-for-like and not part of the cost comparison.
    with conn.cursor() as cur:
        cur.execute(f"""
            CREATE INDEX idx_{demo_settings.TABLE_NAME}_department
                ON {demo_settings.TABLE_NAME} (department);
        """)
    conn.commit()


def sample_emp_nos(conn, size):
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT emp_no FROM {demo_settings.TABLE_NAME} "
            f"ORDER BY random() LIMIT %s;",
            (size,),
        )
        return [row[0] for row in cur.fetchall()]


def backfill_sample(conn, emp_nos):
    departments = ["Marketing", "Sales", "Engineering", "Human Resources", "Finance", "Services", "Other"]
    titles = ["Director", "Manager", "Senior Staff", "Staff", "Other"]
    hobbies_pool = ["movies", "cycling", "singing", "running", "hiking", "photography", "reading", "sleeping"]
    base_birth_year = 1975

    updates = []
    for emp_no in emp_nos:
        hobbies = [random.choice(hobbies_pool) for _ in range(1 + int(1000 * random.random()) % 3)]
        updates.append((
            datetime.date(
                int(base_birth_year + random.choice(range(15))),
                int(1 + random.choice(range(11))),
                int(1 + random.choice(range(28))),
            ),
            random.choice(departments),
            random.choice(titles),
            hobbies,
            emp_no,
        ))

    with conn.cursor() as cur:
        # One UPDATE per employee row, hobbies included: like MongoDB's $set,
        # each employee is written atomically in a single row.
        execute_values(
            cur,
            f"""UPDATE {demo_settings.TABLE_NAME} AS t SET
                    birth_date = data.birth_date,
                    department = data.department,
                    title      = data.title,
                    hobbies    = data.hobbies
                FROM (VALUES %s) AS data (birth_date, department, title, hobbies, emp_no)
                WHERE t.emp_no = data.emp_no;""",
            updates,
            template="(%s::date, %s, %s, %s::text[], %s)",
        )
    conn.commit()


if __name__ == "__main__":
    conn = None
    try:
        conn = get_connection()
        print("Connected to PostgreSQL\n")

        print("Applying schema change v1 (timed per step):")
        timed("ADD COLUMN birth_date, department, title, hobbies TEXT[]", lambda: add_columns(conn))
        timed(f"CREATE INDEX on {demo_settings.TABLE_NAME}.department (same as MongoDB)",
              lambda: add_department_index(conn))

        print(f"\nFilling in {demo_settings.NUM_SAMPLING} of {demo_settings.NUM_ITEMS} rows "
              f"(same sparse-population pattern as the Mongo proof) ...")
        emp_nos = sample_emp_nos(conn, demo_settings.NUM_SAMPLING)
        start = time.perf_counter()
        backfill_sample(conn, emp_nos)
        elapsed = time.perf_counter() - start
        print(f"  [{elapsed * 1000:8.2f} ms] UPDATE sample rows (hobbies stored as a text[] array)")

        print("\nOperation completed successfully!!!")
        print(f"Note: the other {demo_settings.NUM_ITEMS - demo_settings.NUM_SAMPLING} rows now have "
              f"NULL birth_date/department/title/hobbies.")

    except psycopg2.OperationalError as e:
        print("Could not connect to PostgreSQL:", e)
    finally:
        if conn:
            conn.close()
