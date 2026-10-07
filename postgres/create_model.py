#!/usr/bin/env python3

import datetime
import random

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


def create_schema(conn):
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {demo_settings.HOBBIES_TABLE_NAME};")
        cur.execute(f"DROP TABLE IF EXISTS {demo_settings.TABLE_NAME};")
        cur.execute(f"""
            CREATE TABLE {demo_settings.TABLE_NAME} (
                emp_no          INTEGER PRIMARY KEY,
                first_name      VARCHAR(50) NOT NULL,
                last_name       VARCHAR(50) NOT NULL,
                gender          CHAR(1)     NOT NULL,
                annual_salary   INTEGER     NOT NULL,
                hire_date       DATE        NOT NULL
            );
        """)
    conn.commit()


def create_row(item):
    # same value pools/ranges as ../mongodb/create_model.py, for a like-for-like comparison
    female_names = ["Ana", "Elizabeth", "Helen", "Diana", "Maria", "Patricia", "Teresa"]
    male_names = ["Alex", "Bart", "Charles", "John", "Michael", "Paul", "Peter"]
    first_names = {"F": female_names, "M": male_names}
    last_names = ["Anderson", "Brown", "Davis", "Jones", "Johnson", "Smith", "Williams"]
    gender = random.choice(["F", "M"])
    base_emp_no = 1000
    base_salary = 40000
    base_hire_year = 2000

    return (
        int(base_emp_no + item),
        random.choice(first_names[gender]),
        random.choice(last_names),
        gender,
        base_salary + round(random.random() * base_salary),
        datetime.date(
            int(base_hire_year + random.choice(range(17))),
            int(1 + random.choice(range(11))),
            int(1 + random.choice(range(28))),
        ),
    )


def generate_data(conn, items):
    rows = [create_row(n) for n in range(items)]
    with conn.cursor() as cur:
        execute_values(
            cur,
            f"""INSERT INTO {demo_settings.TABLE_NAME}
                (emp_no, first_name, last_name, gender, annual_salary, hire_date)
                VALUES %s""",
            rows,
        )
    conn.commit()


if __name__ == "__main__":
    conn = None
    try:
        conn = get_connection()
        print("Connected to PostgreSQL")

        print("(Re)creating table:", demo_settings.TABLE_NAME)
        create_schema(conn)

        print("Creating new", demo_settings.NUM_ITEMS, "rows ...")
        generate_data(conn, demo_settings.NUM_ITEMS)

        print("Operation completed successfully!!!")

    except psycopg2.OperationalError as e:
        print("Could not connect to PostgreSQL:", e)
    finally:
        if conn:
            conn.close()
