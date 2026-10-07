#!/usr/bin/env python3

"""
Equivalent of ../mongodb/microservice_one.py: an "existing" service that only
ever reads the original columns. Should keep running unmodified across
alter_model.py, exactly like its Mongo counterpart.
"""

import random
import time

import psycopg2

import demo_settings


def get_connection():
    return psycopg2.connect(
        host=demo_settings.PG_HOST,
        port=demo_settings.PG_PORT,
        dbname=demo_settings.PG_DBNAME,
        user=demo_settings.PG_USER,
        password=demo_settings.PG_PASSWORD,
    )


if __name__ == "__main__":
    conn = None
    try:
        conn = get_connection()
        # Read-only report script: autocommit so no implicit transaction is
        # left open (and no lock held) between queries during the sleep.
        conn.autocommit = True
        print("Microservice One - connected to PostgreSQL\n")

        with conn.cursor() as cur:
            cur.execute(f"SELECT emp_no FROM {demo_settings.TABLE_NAME};")
            ids = [row[0] for row in cur.fetchall()]

        while True:
            print("Running employees report (microservice one)")
            with conn.cursor() as cur:
                for _ in range(5):
                    emp_no = random.choice(ids)
                    cur.execute(
                        f"SELECT first_name, last_name, gender "
                        f"FROM {demo_settings.TABLE_NAME} WHERE emp_no = %s;",
                        (emp_no,),
                    )
                    print(cur.fetchone())

            print("\n...")
            time.sleep(5)

    except psycopg2.OperationalError as e:
        print("Could not connect to PostgreSQL:", e)
    except KeyboardInterrupt:
        pass
    finally:
        if conn:
            conn.close()
