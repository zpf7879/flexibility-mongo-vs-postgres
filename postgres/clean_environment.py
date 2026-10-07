#!/usr/bin/env python3

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
        print("Connected to PostgreSQL")

        with conn.cursor() as cur:
            print(f"Dropping table: {demo_settings.HOBBIES_TABLE_NAME}")
            cur.execute(f"DROP TABLE IF EXISTS {demo_settings.HOBBIES_TABLE_NAME};")
            print(f"Dropping table: {demo_settings.TABLE_NAME}")
            cur.execute(f"DROP TABLE IF EXISTS {demo_settings.TABLE_NAME};")
        conn.commit()

        print("Operation completed successfully!!!")

    except psycopg2.OperationalError as e:
        print("Could not connect to PostgreSQL:", e)
    finally:
        if conn:
            conn.close()
