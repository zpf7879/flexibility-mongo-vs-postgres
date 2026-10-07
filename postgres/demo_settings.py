# Local Docker Postgres credentials only - matches docker-compose.yml.
# Do not point this at a shared, remote, or production database.
PG_HOST = "localhost"
PG_PORT = 5432
PG_DBNAME = "flexible_rdbms"
PG_USER = "main_user"
PG_PASSWORD = "main_password"

NUM_ITEMS = 10000
NUM_SAMPLING = 1000
TABLE_NAME = "employees"
HOBBIES_TABLE_NAME = "employee_hobbies"
