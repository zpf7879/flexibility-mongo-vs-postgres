# Evolvability on a Live Database: MongoDB vs. PostgreSQL

> **Who this post is for.** This comparison assumes a PostgreSQL team that keeps its structured, business-significant data **normalized**, in typed columns and related tables, rather than putting evolving data in a JSONB column. That's a deliberate, common choice. Once the hobbies in our experiment have attributes of their own (change v2 below), a PostgreSQL DBA would typically explain it like this:
>
> *"I would use a child table rather than JSONB because the relationship between an employee and their hobbies is a clear one-to-many relationship with well-defined, typed attributes such as name, since, and level. A relational child table makes these fields easier to query, index, validate, constrain, and maintain as the system evolves, while keeping the data semantics explicit to PostgreSQL. This is a widely used and well-established practice in the PostgreSQL and relational database world: use normalized tables for stable, business-significant data, and reserve JSONB primarily for flexible or genuinely semi-structured data."*
>
> If your team would reach for JSONB instead, the trade-offs are different, and they get their own post (coming soon).

"MongoDB's flexible schema lets you change your data model without downtime."

Say that to someone who runs PostgreSQL and you'll usually hear: *"So does Postgres."*

They're mostly right. Since PostgreSQL 11, adding a column is a metadata-only change that finishes in milliseconds. No table rewrite, no maintenance window. If "flexible schema" only meant "no downtime," there wouldn't be much of a story.

So we ran an experiment. We made the same two data model changes on both databases, with services running against them the whole time, and watched where the differences actually showed up. They weren't in uptime. They were in **what happens when data changes shape, the locks you have to think about, and the rollout and process around the change.**

In one word, the difference isn't *availability*. It's **evolvability**: how easily your data model keeps changing as the application does.

This post walks through what we found, and then shows you how to run the demo yourself.

---

## The experiment

Both databases start with the same data: **10,000 employees**, each with an employee number, name, gender, hire date and salary.

Then the business asks for more, in two releases:
- **Change v1:** a **department**, a **job title**, a **birth date**, and a list of **hobbies** (one to three per person, like `["cycling", "reading"]`).
- **Change v2:** a release later, each hobby needs **more detail**: a skill level and the year the person started, like `{name: "cycling", level: "advanced", since: 2019}`.

Why not a child table for hobbies right away? In change v1, hobbies are just labels, like tags, with no attributes of their own. That's neither "well-defined, typed attributes" nor "business-significant data" (see the DBA's reasoning at the top), so even a team that normalizes would usually keep them in a simple `text[]` array. When they gain attributes in v2, the same rule moves them into a child table.

To keep it realistic, only **1,000 employees** get the new information right away. The rest will be filled in over time, which is how new data usually arrives.

Two small services read the data the whole time:
- **`microservice_one.py`** is the *old* service. It only knows about names and gender, and it keeps running throughout.
- **`microservice_two.py`** is the *new* service. It reports employees by department, including their hobbies.

Why PostgreSQL? Because it's the strongest opponent, and we gave it the best design at every step. A comparison against an old MySQL version or SQLite, or against a deliberately clumsy schema, would be easy to win and easy to dismiss.

Here's change v1 in MongoDB, from `alter_model.py`. The update is applied to 1,000 random documents, followed by an index for the new service's department lookups:

```python
collection.update_one(
    {"emp_no": emp_no},
    {"$set": {"department": "Sales", "title": "Manager",
              "birth_date": birth_date, "hobbies": ["cycling", "reading"]}},
)

collection.create_index("department")
```

And here it is in PostgreSQL, with the hobby labels in a native `text[]` array:

```sql
ALTER TABLE employees
    ADD COLUMN birth_date DATE,
    ADD COLUMN department VARCHAR(50),
    ADD COLUMN title      VARCHAR(50),
    ADD COLUMN hobbies    TEXT[];

CREATE INDEX ON employees (department);          -- same index as MongoDB

UPDATE employees
SET department = 'Sales', title = 'Manager', birth_date = '1982-03-14',
    hobbies = ARRAY['cycling', 'reading']
WHERE emp_no = 10001;                            -- one row, one atomic write
```

Honestly? That's nearly a draw. Same index, one atomic write per employee, and reading an employee back is a single-table `SELECT` in Postgres just as it's a single document in MongoDB. The one edge is the code itself: the MongoDB version is shorter and easier to read and maintain. There's no schema step to write, review and keep in sync with the application; the new fields simply appear in the `$set`. Change v2 is where things get interesting. But first, let's be fair about what isn't different.

---

## First, what's *not* different

It's worth saying this up front, because a lot of MongoDB-vs-SQL comparisons get it wrong:

- **Neither database needs downtime.** PostgreSQL's `ALTER TABLE ... ADD COLUMN` finishes in milliseconds. Our `alter_model.py` times every step so you can see it.
- **The old service keeps running in both.** `microservice_one.py` never notices either change, in either database, and its code doesn't change.
- **A flat list is fine in both.** For change v1, a Postgres `text[]` column is as easy to write, read and index as a MongoDB array.
- **Querying works fine in both.** Finding employees with a given hobby, or listing the departments that exist, is easy and fast in either database.

If your argument for MongoDB is "relational databases need downtime to add a column," you'll lose the room. So where *is* the difference?

---

## Difference 1: when a list grows up

Change v2 asks for a level and a start year on every hobby. Each item in the list now has its own fields.

**PostgreSQL.** A `text[]` can't hold that. For a team that keeps its data normalized (see the note at the top), the answer is a **child table**: one row per hobby with typed columns, linked back to the employee by a foreign key. Moving the existing hobbies into it on a live system takes a staged rollout, covered in [Difference 3](#difference-3-the-rollout-and-the-process-around-it). Here, let's look at the code once the move is done.

**The join is permanent.** Every save of an employee's hobbies is now a transaction across two tables. Because there's no "set this list" for a child table, you delete the old hobby rows and insert the new ones:

```python
with conn:                                   # one transaction
    with conn.cursor() as cur:
        # Lock this employee's row: one writer per employee at a time
        cur.execute("SELECT 1 FROM employees WHERE emp_no = %s FOR UPDATE", (emp_no,))
        cur.execute("DELETE FROM employee_hobbies WHERE emp_no = %s", (emp_no,))
        execute_values(cur,
            "INSERT INTO employee_hobbies (emp_no, hobby, level, since) VALUES %s",
            [(emp_no, h["name"], h["level"], h["since"]) for h in hobbies])
```

Every line is there for a reason. The `FOR UPDATE` line, for example, stops two simultaneous saves of the same employee from *merging* their hobby lists (the [deep dive](DEEP_DIVE.md#1-writes-one-document-vs-a-multi-table-transaction) has a script that reproduces it). In MongoDB, the same save is still one `$set` on one document.

Reading is a join plus an aggregate to fold one-row-per-hobby back into one employee:

```sql
SELECT e.emp_no, e.department,
       json_agg(json_build_object('name', h.hobby, 'level', h.level, 'since', h.since)) AS hobbies
FROM employees e
LEFT JOIN employee_hobbies h ON h.emp_no = e.emp_no
WHERE e.emp_no = 10001
GROUP BY e.emp_no;
```

In MongoDB it's still `find_one({"emp_no": 10001})`.

**It compounds, too.** Give a second list (say, `skills`) the same treatment and join both, and 3 hobbies × 4 skills gives you 12 rows per employee and duplicated values, unless you rewrite the query. Give the hobbies their own sub-list (events attended, say), and that's yet another table. In MongoDB, both are just more fields in the same document.

**"Why not a child table from the start?"** If hobbies had been in a child table since v1, change v2 would be an instant `ADD COLUMN` on that table. But the cost only moves earlier: the same joins and multi-table transactions from day one, for what was just a list of labels. A normalized team either pays early (joins from v1) or pays later (a staged rollout at v2, then the same joins). MongoDB pays neither: the labels are an array, then an array of objects, in the same document, read and written in one operation.

**The takeaway:** a flat list is a draw. The difference appears when data *changes shape*, which is exactly what agile applications keep doing. In MongoDB, the new shape is a new field layout in the same document. In PostgreSQL, it's a new table, with joins and multi-table transactions from then on, and a staged rollout to get there.

---

## Difference 2: locks on a live table

`ALTER TABLE` is fast, but it needs an **exclusive lock** on the table for that brief moment. That's where things get interesting.

If *any* transaction has the table open (even one that's just sitting idle because someone forgot to commit), the `ALTER` has to wait. And PostgreSQL queues lock requests in order. So every ordinary `SELECT` that arrives after the `ALTER` waits *behind* it, including the simple reads from the old service.

In other words: **one forgotten transaction plus one harmless-looking `ADD COLUMN` can freeze your old service's reads.**

You can see it happen with this query in `psql`:

```
 pid  | blocked_by | state               | query
------+------------+---------------------+----------------------------------
 4101 | {}         | idle in transaction | SELECT ... FOR UPDATE
 4102 | {4101}     | active              | ALTER TABLE employees ADD COLUMN …
 4103 | {4102}     | active              | SELECT count(*) FROM employees;
```

Read it from the bottom up: an ordinary read (4103) is stuck behind the schema change (4102), which is stuck behind an idle transaction (4101) that has nothing to do with either of them.

There's a standard fix. Tell the `ALTER` to give up quickly and try again later, instead of blocking everyone:

```sql
SET lock_timeout = '1s';
ALTER TABLE employees ADD COLUMN department VARCHAR(50);
-- ERROR: canceling statement due to lock timeout  -> wait, then retry
```

It works, and our demo shows it working. But that's the point: **someone has to know to write it.**

And change v2 needs that care twice more:
- **Creating the child table.** The new `employee_hobbies` table is empty, but its foreign key points at `employees`, so creating it takes a lock on `employees` too. That lock doesn't block reads, but it does block writes: it waits for any transaction that's in the middle of writing to `employees`, and new writes queue behind it. The old service keeps reading, but anything saving employees stalls until the lock is granted.
- **Dropping the old `hobbies` column** at the end of the migration. Like `ADD COLUMN`, `DROP COLUMN` is fast but needs the same exclusive lock, so it can queue behind an idle transaction and freeze reads exactly as shown above.

Each one needs its own `lock_timeout` and retry. Adding or reshaping fields in MongoDB involves no schema change and no table lock, so there's nothing to get wrong.

---

## Difference 3: the rollout and the process around it

This is the biggest real-world cost, and it isn't about milliseconds at all.

**A change like v2 can't land in one release.** The hobbies currently live in the array, and the app is reading and writing them there *right now*. Moving them while everything keeps running takes a staged migration, the classic "expand and contract":

| Step | PostgreSQL | MongoDB |
|---|---|---|
| 1. Make room for the new shape | `CREATE TABLE employee_hobbies` + foreign key + index | Nothing to do |
| 2. Deploy an app that writes the new shape | Must write **both** the array and the new table, in one transaction: until reads switch in step 4, every reader, including old app instances and any rollback, still uses the array | Writes the new shape with one `$set` |
| 3. Convert existing data | **Required:** copy every hobby from the array into the table, in batches, before anyone reads from the table | **Optional:** convert documents gradually, or leave old ones as they are |
| 4. Switch reads | Deploy an app that reads with a `JOIN` | No switch; the same `find()` returns both shapes |
| 5. Clean up | Remove the dual writes, deploy again, then `DROP COLUMN hobbies` | Nothing required. Optionally, convert any documents still in the old shape so the code that reads the old shape can be removed |

**MongoDB isn't free here, either.** A document converted to the new shape sits right next to one that still has plain strings, and any code that reads hobbies has to accept both until the conversion is done. In our demo that's four lines in `microservice_two.py`:

```python
def describe(hobby):
    if isinstance(hobby, str):          # old shape: "cycling"
        return hobby
    return f"{hobby['name']} ({hobby['level']}, since {hobby['since']})"   # new shape
```

So both databases need a careful rollout. The difference is what that rollout is made of. In MongoDB it's a few lines of tolerant reading code, and old and new data can live side by side for as long as you like. In PostgreSQL it's a schema change, dual writes to two places, a data copy that has to finish before reads can switch, and at least two more deploys.

**Then each step goes through the release process.** In most teams, a relational schema change goes through:
- a **migration tool** (Flyway, Liquibase, Alembic and so on),
- **code review** of the migration script,
- a **rehearsal** in a staging environment, and
- **release coordination**, so the database change lands before the app that needs it.

That last point is easy to see if you start the new service *before* the change. In MongoDB its query is valid and simply returns no results until the data arrives. In PostgreSQL it fails with `UndefinedColumn`, because the column doesn't exist yet. On its own that's a small difference: both can be handled gracefully, and migration tools such as Flyway often run the schema change when the app starts, which settles the order for you. It grows with ORMs, where every query on a changed entity selects the new column and fails until it exists, and with several services sharing one database, where someone has to own the migration and make sure it lands first.

Change v2 goes through that process *more than once*: the expand step, the dual-write release, the copy, the read switch and the cleanup each need scheduling. That's often weeks of calendar time. With MongoDB, the data change ships with the application code that uses it. You still review and test that code, of course, but there's no separate database release to schedule.

---

## Try it yourself: running the demo

The demo runs both databases side by side so the audience can see each difference happen. Allow about 15 minutes to set up and 20 minutes to run it.

### Setup

The project layout:
```
02-flexible-vs-postgres/
├── README.md            this post, including the walkthrough
├── DEEP_DIVE.md         details and objection handling
├── docker-compose.yml   PostgreSQL 16
├── requirements.txt     pymongo + psycopg2
├── mongodb/             the MongoDB scripts
└── postgres/            the PostgreSQL scripts (same names, same steps)
```

**Python.** From this directory, install both drivers:
```bash
pip3 install -r requirements.txt
```

**MongoDB.** Create an M10 Atlas cluster with a database user that can read and write any database, and allow your laptop's IP address (the original proof's [setup steps](../02/README.md#setup) walk through this). Then put the connection string in an environment variable, in every terminal you'll run MongoDB scripts from:
```bash
export MONGODB_URI="mongodb+srv://main_user:<password>@<your-cluster>/"
```
`mongodb/demo_settings.py` reads it from there, so no password ever goes into a file.

**PostgreSQL.** From this directory, start Postgres 16 in Docker:
```bash
docker compose up -d
```
It listens on `localhost:5432` with the user `main_user`, password `main_password` and database `flexible_rdbms`, as set in `postgres/demo_settings.py`. These are local Docker credentials only. If you point the demo at a shared or remote Postgres instead, change them and don't commit real ones.

**Screen layout.** Arrange terminals in two columns: MongoDB on the left (`cd mongodb`) and PostgreSQL on the right (`cd postgres`). Keep one extra terminal for a `psql` session:
```bash
docker exec -it flexible_rdbms_postgres psql -U main_user -d flexible_rdbms
```

### Step 1: load the data

In **both** `mongodb/` and `postgres/`:
```bash
./create_model.py
```
Each loads the same 10,000 employees. For MongoDB, it's worth opening the collection in Compass so the audience can see the documents.

### Step 2: start the old service

In **both** `mongodb/` and `postgres/`, in their own terminals:
```bash
./microservice_one.py
```
Leave these running for the rest of the demo. They print a few employee names every five seconds.

### Step 3: deploy the new service early (part of Difference 3)

Before changing anything, deploy the new service early. In **both** `mongodb/` and `postgres/`:
```bash
./microservice_two.py
```

- **PostgreSQL** stops with an `UndefinedColumn` error and a message explaining that the schema change must come first.
- **MongoDB** starts fine. Its query returns an empty list, so it prints that no employee has a department yet and checks again every five seconds. **Leave it running.**

If you'd like to show the raw query too, run it in `mongosh` or Compass:
```javascript
db.employees.distinct("department")   // returns []
```

### Step 4: apply change v1

In **both** `mongodb/` and `postgres/`:
```bash
./alter_model.py
```
Point out:
- **PostgreSQL** prints a timing for each step. The `ADD COLUMN` (including `hobbies TEXT[]`) takes milliseconds, which confirms there's no downtime. At the end it notes that 9,000 rows now have `NULL` in the new columns.
- **MongoDB** has no schema step at all. Refresh Compass to show that some documents now have `department`, `title`, `birth_date` and `hobbies`, and others don't. Within five seconds, the MongoDB `microservice_two.py` you left running in Step 3 picks up the new departments and starts reporting, with no restart.
- Both scripts create the same `department` index, and both store hobbies as a simple list. Call it a draw.

### Step 5: check the old service (what's *not* different)

Look at both `microservice_one.py` terminals. Both are still running and never had to change. Say so plainly: this is a draw, and admitting it makes the rest of the comparison more credible.

### Step 6: run the new service on change v1

The MongoDB `microservice_two.py` is already running from Step 3. Now start the PostgreSQL one, which can only start after the schema change:
```bash
./microservice_two.py
```
Both print employees by department, with their hobbies. Open the two files side by side: each is a single query on a single table or collection. Another draw, and it sets up the next step.

### Step 7: change v2, hobbies grow up (Differences 1 and 3)

Leave both `microservice_two.py` services running. In **both** `mongodb/` and `postgres/`:
```bash
./alter_model_v2.py
```

- **MongoDB** converts about half of the employees' hobbies to the new `{name, level, since}` shape and leaves the rest as strings. It prints how many documents have each shape. Within five seconds, the running `microservice_two.py` shows both kinds in its output, such as `cycling (advanced, since 2019)` next to plain `reading`, with no restart. Show the four-line `describe()` function that makes this work, and be clear that it's the cost on the MongoDB side.
- **PostgreSQL** prints the five migration steps from the table in Difference 3, and runs the two that a script can do: it creates the child table (step 1) and copies every hobby across in batches (step 3), timing each. Point out the two steps it *can't* do for you: deploying dual-write code (step 2) and switching reads (step 4).

Now play step 4 for Postgres: stop its `microservice_two.py` and start the version that reads from the new table:
```bash
./microservice_two_v2.py
```
Open `microservice_two_v2.py` and show the query: a `LEFT JOIN`, `json_agg`, `GROUP BY` before `LIMIT` (or `LIMIT` would count hobby rows instead of employees), and `FILTER`/`COALESCE` so employees without hobbies get `[]`. Compare it with MongoDB's `microservice_two.py`, which hasn't changed at all.

**Optional finale.** Run the cleanup step in each:
```bash
./alter_model_v2.py --contract      # in postgres/: DROP COLUMN hobbies
./alter_model_v2.py --convert-all   # in mongodb/: convert the remaining strings
```
If the old Postgres `microservice_two.py` is still running when you drop the column, it fails with `UndefinedColumn`. That's why the cleanup has to wait for every old deploy to be gone.

### Step 8: show the lock queue (Difference 2)

In the `psql` terminal, get this query ready to run (re-run it with `\watch 1` to refresh every second):
```sql
SELECT pid, pg_blocking_pids(pid) AS blocked_by, state,
       wait_event_type, left(query, 50) AS query
FROM pg_stat_activity
WHERE datname = current_database() AND pid <> pg_backend_pid();
```
Then, in `postgres/`:
```bash
./lock_contention_demo.py
```
It runs two scenarios of about 10 seconds each:
1. **Plain `ALTER TABLE`.** An open transaction holds the table, the `ALTER` waits for it, and a reader (just like the old service) stalls behind the `ALTER` for several seconds. This is when the `psql` window shows the blocking chain.
2. **With `lock_timeout` and retry.** The same `ALTER` gives up after one second and retries. The reader's longest wait drops to about a second.

The script ends by printing the longest read wait from each scenario, side by side. There's no MongoDB equivalent to run here, and that's the point: neither change needed a schema change.

### Step 9: close with the process (Difference 3)

Finish by asking the audience how a schema change reaches production in their team today: what tool, what reviews, how many environments, and who coordinates the release. Then ask how many times change v2 would go through that process. That conversation usually makes Difference 3 better than any slide.

### Cheat sheet

| Point | What to run | What to show |
|---|---|---|
| No downtime in either | `alter_model.py` (both) | Postgres `ADD COLUMN` timings in milliseconds |
| Old service unaffected | `microservice_one.py` (both) | Both keep running through both changes |
| Flat list is a draw | `microservice_two.py` (both) after v1 | One query on one table or collection, each |
| 1. A list grows up | `alter_model_v2.py` (both), then Postgres `microservice_two_v2.py` | MongoDB: mixed shapes, no restart. Postgres: a child table, then a `JOIN` in every read |
| Deploy order (part of 3) | `microservice_two.py` (both) before v1 | Postgres fails with `UndefinedColumn`; MongoDB waits, then starts reporting on its own |
| 2. Locks | `lock_contention_demo.py` + `psql` | The blocking chain, then `lock_timeout` fixing it |
| 3. Process | Talk | The 5 rollout steps v2 needs (2 of them app deploys), and how many times v2 would go through their release process |

### Clean up

Run these from the project directory (`02-flexible-vs-postgres/`), where `docker-compose.yml` lives. `docker compose` only finds it there:
```bash
cd mongodb  && ./clean_environment.py && cd ..
cd postgres && ./clean_environment.py && cd ..
docker compose down -v
```

---

## Wrapping up

PostgreSQL handles "add a column" very well, and even "add a list of strings". It's worth saying so. For a team that keeps its data normalized, the difference is in **evolvability**, and it shows up when the data keeps evolving:

> **When a list's items grow their own fields, a normalized PostgreSQL design needs a staged migration: a new table, dual writes, a data copy and several coordinated deploys, followed by joins and multi-table transactions from then on. Each step also brings the locks and release process around it. In MongoDB, the new shape lives in the same document next to the old one, your code reads both, and you convert at your own pace.**

---

## Further reading

- **[The deep dive: details and objection handling](DEEP_DIVE.md).** Everything this post skips: the full migration in SQL, step by step; what the MongoDB side really involves; the two-window lost-update script; the 12-row join; how a foreign key can break an old batch job; what MongoDB *doesn't* win; and the lock details a DBA will ask about. Read this before presenting to a technical audience.
- **MongoDB vs. PostgreSQL JSONB** *(coming soon)*: the same comparison for teams that store evolving data in JSONB instead of normalizing it.
