#!/usr/bin/env python3

"""
Equivalent of ../mongodb/microservice_two.py: a "newly added" service that
reads the new department/title columns plus the hobbies added by
alter_model.py (v1), where hobbies are a flat `text[]` array column.

With the array, reading an employee is a plain single-table SELECT, the
same as reading a MongoDB document. Once hobbies need structure
(alter_model_v2.py), reads move to the child table: see
microservice_two_v2.py.

Unlike the MongoDB version, this script CANNOT be started before
alter_model.py has run -- `department` doesn't exist as a column yet, so
the query fails outright rather than just returning no matches. That
asymmetry (DB migration and app deploy must be sequenced) is itself part
of the comparison -- see ../README.md.
"""

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


QUERY = f"""
    SELECT emp_no, first_name, last_name, department, title, hobbies
    FROM {demo_settings.TABLE_NAME}
    WHERE department = %s
    LIMIT 5;
"""


if __name__ == "__main__":
    conn = None
    try:
        conn = get_connection()
        # Read-only report script: autocommit so no implicit transaction is
        # left open (and no lock held) between queries during the sleep.
        conn.autocommit = True
        print("Microservice Two (v1: hobbies from the text[] column) - connected to PostgreSQL\n")

        with conn.cursor() as cur:
            try:
                cur.execute(
                    f"SELECT DISTINCT department FROM {demo_settings.TABLE_NAME} "
                    f"WHERE department IS NOT NULL;"
                )
            except psycopg2.errors.UndefinedColumn:
                print("ERROR: column 'department' does not exist yet.")
                print("Run alter_model.py first -- unlike the MongoDB version of this "
                      "proof, this microservice cannot be deployed ahead of the schema "
                      "change; the DDL and the app deploy must be sequenced.")
                raise SystemExit(1)
            departments = [row[0] for row in cur.fetchall()]

        print("..departments..", departments)
        print()

        while True:
            print("Running employees report (microservice two, v1)")
            dept = random.choice(departments)
            print("--ENQUIRY FOR DEPARTMENT: " + dept)
            with conn.cursor() as cur:
                try:
                    cur.execute(QUERY, (dept,))
                except psycopg2.errors.UndefinedColumn:
                    print("ERROR: column 'hobbies' no longer exists -- "
                          "alter_model_v2.py --contract has dropped it.")
                    print("This v1 service still reads the array, so the contract step "
                          "had to wait until it was replaced by microservice_two_v2.py.")
                    raise SystemExit(1)
                for emp_no, first, last, department, title, hobbies in cur.fetchall():
                    print(f"{emp_no} {first} {last} | {department} | {title} | hobbies: {hobbies}")

            print("...\n")
            time.sleep(5)

    except psycopg2.OperationalError as e:
        print("Could not connect to PostgreSQL:", e)
    except KeyboardInterrupt:
        pass
    finally:
        if conn:
            conn.close()
