#!/usr/bin/env python3

"""
Microservice two after change v2 (step 4 of alter_model_v2.py): hobbies
have structure ({hobby, level, since}) and live in the employee_hobbies
child table, so every read is a JOIN plus an aggregate to rebuild each
employee's list.

Compare ../mongodb/microservice_two.py: the MongoDB service still reads one
document per employee, and handles both hobby shapes (plain strings and
objects) while documents are converted.
"""

import json
import random
import time

import psycopg2
import psycopg2.errors

import demo_settings


def get_connection():
    return psycopg2.connect(
        host=demo_settings.PG_HOST,
        port=demo_settings.PG_PORT,
        dbname=demo_settings.PG_DBNAME,
        user=demo_settings.PG_USER,
        password=demo_settings.PG_PASSWORD,
    )


# GROUP BY must come before LIMIT, or LIMIT would count hobby rows instead of
# employees. FILTER + COALESCE turn "no hobby rows" into [] rather than [null].
QUERY = f"""
    SELECT e.emp_no, e.first_name, e.last_name, e.department, e.title,
           COALESCE(
               json_agg(json_build_object('name', h.hobby, 'level', h.level, 'since', h.since)
                        ORDER BY h.id)
                   FILTER (WHERE h.id IS NOT NULL),
               '[]'::json) AS hobbies
    FROM {demo_settings.TABLE_NAME} e
    LEFT JOIN {demo_settings.HOBBIES_TABLE_NAME} h ON h.emp_no = e.emp_no
    WHERE e.department = %s
    GROUP BY e.emp_no
    LIMIT 5;
"""


def describe(hobby):
    if hobby["level"] is None:
        return hobby["name"]
    return f"{hobby['name']} ({hobby['level']}, since {hobby['since']})"


if __name__ == "__main__":
    conn = None
    try:
        conn = get_connection()
        conn.autocommit = True
        print("Microservice Two (v2: hobbies from the child table) - connected to PostgreSQL\n")

        with conn.cursor() as cur:
            cur.execute(
                f"SELECT DISTINCT department FROM {demo_settings.TABLE_NAME} "
                f"WHERE department IS NOT NULL;"
            )
            departments = [row[0] for row in cur.fetchall()]

        print("..departments..", departments)
        print()

        while True:
            print("Running employees report (microservice two, v2)")
            dept = random.choice(departments)
            print("--ENQUIRY FOR DEPARTMENT: " + dept)
            with conn.cursor() as cur:
                try:
                    cur.execute(QUERY, (dept,))
                except psycopg2.errors.UndefinedTable:
                    print(f"ERROR: table '{demo_settings.HOBBIES_TABLE_NAME}' does not exist yet.")
                    print("Run alter_model_v2.py first: reads can only switch to the child "
                          "table after it exists and the existing hobbies have been copied.")
                    raise SystemExit(1)
                for emp_no, first, last, department, title, hobbies in cur.fetchall():
                    if isinstance(hobbies, str):
                        hobbies = json.loads(hobbies)
                    shown = [describe(h) for h in hobbies]
                    print(f"{emp_no} {first} {last} | {department} | {title} | hobbies: {shown}")

            print("...\n")
            time.sleep(5)

    except psycopg2.OperationalError as e:
        print("Could not connect to PostgreSQL:", e)
    except KeyboardInterrupt:
        pass
    finally:
        if conn:
            conn.close()
