# MongoDB vs. PostgreSQL Schema Changes: The Deep Dive

*Companion to [Evolvability on a Live Database: MongoDB vs. PostgreSQL](README.md). The blog post gives the overview. This article has the details, the worked SQL, and how to answer the objections a DBA will raise. Read it before presenting the comparison to a technical audience.*

**Contents**
- [Why PostgreSQL is the comparison database](#why-postgresql-is-the-comparison-database)
- [Hobbies in depth](#hobbies-in-depth): from `text[]` to a child table, the migration step by step, what the MongoDB side involves, living with the child table, and what MongoDB does not win
- [Sparse data and deploy order: the details](#sparse-data-and-deploy-order-the-details)
- [Locks: the details for the DBA conversation](#locks-the-details-for-the-dba-conversation)

## Why PostgreSQL is the comparison database

- **Credibility.** It's the strongest opponent to pick. Comparing against MySQL 5.7 or SQLite lets a prospect's DBA dismiss the exercise as a straw man. Beat Postgres fairly and the argument holds everywhere.
- **Reproducible.** No license and no install steps: `docker compose up` fits the style of the rest of `pov-proof-exercises`, where this proof is just Python and a URI.
- **It's the actual competitor.** Most "why not just stay relational" conversations in these PoVs are Postgres conversations, and the "we'll just use a JSONB column" objection will come up regardless. Better to meet it inside the proof than in Q&A.

**Don't build the comparison on downtime.** Since PostgreSQL 11, `ALTER TABLE ... ADD COLUMN ... DEFAULT <const>` is metadata-only and returns in milliseconds. If the comparison claims otherwise, a competent DBA will discredit the whole demo in one sentence.

**Other databases.** If the prospect runs **Oracle** or **SQL Server**, consider running the live demo there instead: it's more representative for that audience. If they're a **MySQL 8** shop, use that; `ALGORITHM=INSTANT` has real exceptions, and its metadata locking is less forgiving than Postgres's. Keep the Postgres version as the default, repeatable comparison in this repo.

## Hobbies in depth

### From a flat list to structured hobbies

**Change v1: a flat list is a draw.** Hobbies start as a list of strings. PostgreSQL's native `text[]` array is the right tool for that, and it's what `postgres/alter_model.py` uses:

```sql
ALTER TABLE employees ADD COLUMN hobbies text[];                               -- metadata-only, instant
UPDATE employees SET hobbies = ARRAY['movies','cycling'] WHERE emp_no = 10001;  -- one atomic row write
SELECT emp_no, hobbies FROM employees WHERE emp_no = 10001;                     -- no join
CREATE INDEX ON employees USING gin (hobbies);                                  -- if you filter by hobby
SELECT * FROM employees WHERE hobbies @> ARRAY['cycling'];
```

One atomic row write, no join, no multi-statement transaction, and indexable. Concede it. The only small gaps are that there's no built-in `$addToSet` (`array_append` doesn't remove duplicates, so you need a `CASE WHEN NOT 'hiking' = ANY(hobbies) ...` guard) and that array columns are a PostgreSQL feature: MySQL and SQL Server don't have them. Neither is worth arguing about.

**Change v2: list items gain fields.** Each hobby now needs a level and a start year: `{name, level, since}`. A `text[]` can't hold that. A relational design has two options:
- a **child table**, one row per hobby with typed columns, linked by a foreign key. This is what a team that keeps its data normalized would choose, and it's the situation this post assumes (see the note at the top of the README);
- a **JSONB column**, which keeps the list in the row but gives up typed columns, foreign keys and simple indexing. That deserves its own article.

(Arrays of composite types, `CREATE TYPE hobby AS (...)` plus `hobby[]`, also exist, but they're awkward to query, update and index, and rarely used in practice.)

**Objection: "We'd have used a child table from v1."** Some teams would, and then change v2 is trivial:
```sql
ALTER TABLE employee_hobbies ADD COLUMN level VARCHAR(20), ADD COLUMN since SMALLINT;   -- instant, additive
```
No dual writes, no copy, no extra deploys. Concede it. But the cost hasn't gone away, it has moved earlier. From v1 onwards, every save of an employee's hobbies is a multi-table transaction and every read is a join plus an aggregate (sections 1 and 2 below), for what was only a list of labels. That's why many normalizing teams keep simple labels in an array until they gain attributes. Either way the team pays: with joins from day one, or with a migration later and joins after that. MongoDB pays neither. Use this dilemma as the answer, rather than arguing that nobody would normalize early.

### The migration, step by step

The data is live: services are reading and writing hobbies in the array *right now*. Moving it to a child table without downtime is a classic expand-contract migration. `postgres/alter_model_v2.py` runs the database steps and prints the deploy steps between them.

**Step 1: expand.** Create the table. This is additive and fast:

```sql
CREATE TABLE employee_hobbies (
    id      BIGSERIAL PRIMARY KEY,
    emp_no  INTEGER NOT NULL REFERENCES employees(emp_no) ON DELETE CASCADE,
    hobby   VARCHAR(50) NOT NULL,
    level   VARCHAR(20),
    since   SMALLINT
);
CREATE INDEX ON employee_hobbies (emp_no);   -- so joins and FK checks can find an employee's rows
```

The `REFERENCES` clause briefly takes a lock on `employees` that blocks writes (not reads). On a busy table, run it with `lock_timeout` like any other DDL.

**Step 2: deploy dual writes.** Every code path that saves hobbies must now write *both* the array and the table, in one transaction. Until reads switch in step 4, everything that reads hobbies still reads the array: other services and reports, old app instances during a rolling deploy, and the old version if you roll back. Writing both keeps the array current for all of them:

```python
with conn:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM employees WHERE emp_no = %s FOR UPDATE", (emp_no,))
        cur.execute("UPDATE employees SET hobbies = %s WHERE emp_no = %s",
                    ([h["name"] for h in hobbies], emp_no))            # old shape, still read
        cur.execute("DELETE FROM employee_hobbies WHERE emp_no = %s", (emp_no,))
        execute_values(cur,
            "INSERT INTO employee_hobbies (emp_no, hobby, level, since) VALUES %s",
            [(emp_no, h["name"], h["level"], h["since"]) for h in hobbies])   # new shape
```

**Objection: "Why not write only to the new table?"** That's a known alternative: *write new, read new with a fallback*. The new app writes hobbies only to the table, and reads from the table, falling back to the array for employees with no rows yet. The copy in step 3 then fills in the rest, skipping employees that already have rows. It saves the dual-write code, but it only works if:
- **every** reader and writer switches at once: no rolling deploy, and no other service or report still reading the array, or they'll see stale hobbies;
- you accept that rolling back loses every edit made since the switch, because the old version only reads the array;
- every reader carries the fallback logic until the copy is complete.

For a small app with a single service, that can be a reasonable trade. On a shared database, dual writes are the safe default. Either way it's still a staged migration with an ordered copy, which MongoDB doesn't need.

**Where MongoDB needs no dual write at all: consumers that just pass the data through.** Many old consumers don't interpret hobbies. They read them and display them: an API that returns the employee as JSON to a UI, a page that renders the list, a report that prints it.
- **In MongoDB,** the new shape is written to the same field of the same document, so these consumers still get the new data, unchanged and without a deploy. At worst it looks less tidy, such as `{name: "cycling", level: "advanced", since: 2019}` instead of `cycling`, and if that matters, it's visible on screen.
- **In PostgreSQL,** once a writer stops updating the array, the same consumer reads the old column and gets either `NULL`, if the writer clears it, or the old hobbies, if it doesn't. Stale data is the worse case: it looks correct, nothing raises an error, and nobody can tell that the real data has moved to a table the consumer doesn't know about. Preventing that silent failure is what dual writes are for.

This only covers consumers that don't depend on the shape. One that maps hobbies to a typed list of strings (a Java POJO, a Go struct, a Pydantic model), or works on each item as a string, breaks in MongoDB too. It needs the tolerant reader before any new-shape write, as described [below](#what-the-mongodb-side-really-involves). The difference is how each fails: MongoDB fails loudly and only for consumers that care about the shape, while PostgreSQL fails silently for every consumer of the old column.

**Step 3: copy existing data.** Every hobby already in the array has to be copied before anything can read from the table. On a large table, do it in batches so no single transaction runs for long:

```sql
INSERT INTO employee_hobbies (emp_no, hobby)
SELECT emp_no, unnest(hobbies)
FROM employees
WHERE hobbies IS NOT NULL AND emp_no >= 1000 AND emp_no < 2000;   -- repeat per batch
```

If a dual-written employee is saved while the copy runs, the copy and the app can both insert that employee's hobbies. A real migration needs to handle that, for example by skipping employees that already have rows (`AND NOT EXISTS (...)`) and running the copy only after step 2 is fully deployed.

**Step 4: switch reads.** Deploy the version of every reader that uses the table. From now on, reading an employee's hobbies is a join plus an aggregate. In the demo, this is the move from `microservice_two.py` to `microservice_two_v2.py`.

**Step 5: contract (optional).** Like converting the old documents in MongoDB, this step can wait, or never happen. Until it does, every save writes hobbies twice, and the array is a second copy that has to stay in sync. To clean up, remove the dual writes and deploy again. Then, once no deployed code reads or writes the array:

```sql
SET lock_timeout = '2s';
ALTER TABLE employees DROP COLUMN hobbies;
```

Do this too early and any old reader still running fails with `UndefinedColumn`. The demo shows it: run `alter_model_v2.py --contract` while the v1 `microservice_two.py` is still up.

That's one table, two app deploys (dual write, read switch), a batched copy, and a DDL step that needs lock care. The optional cleanup adds a third deploy to remove the dual writes, and a second DDL step. Each deploy goes through the team's normal release process (Difference 3 in the blog post).

### What the MongoDB side really involves

The MongoDB migration is smaller, but it isn't zero. Don't claim it is.

- **Readers must accept both shapes.** After the change, some documents have `"hobbies": ["cycling"]` and others have `"hobbies": [{"name": "cycling", "level": "advanced", "since": 2019}]`. Any code that reads hobbies must handle both until every document is converted. In the demo that's the `describe()` function in `mongodb/microservice_two.py`:
  ```python
  def describe(hobby):
      if isinstance(hobby, str):
          return hobby
      if hobby.get("level") is None:
          return hobby["name"]
      return f"{hobby['name']} ({hobby['level']}, since {hobby['since']})"
  ```
- **Old readers must be gone, or tolerant, before new-shape writes start.** A service that expects strings breaks when it reads an object. So the rollout order still matters: deploy tolerant readers first, then start writing the new shape. That's the MongoDB version of expand-contract, but it lives entirely in application code.
- **Converting the old documents is optional.** You can convert on write (each save writes the new shape), in the background, or in one server-side update. `alter_model_v2.py --convert-all` does the last:
  ```python
  employees.update_many(
      {"hobbies": {"$type": "string"}},
      [{"$set": {"hobbies": {"$map": {
          "input": "$hobbies", "as": "h",
          "in": {"$cond": [{"$eq": [{"$type": "$$h"}, "string"]}, {"name": "$$h"}, "$$h"]},
      }}}}],
  )
  ```
  Each document is converted atomically, and nothing locks the collection.

Side by side:

| | MongoDB | PostgreSQL |
|---|---|---|
| Schema change | None | `CREATE TABLE` + FK + index, optionally `DROP COLUMN` later |
| Writes during the transition | One `$set` with the new shape | Dual write to array and table, in a transaction |
| Existing data | Convert gradually, or not at all | Must copy all of it, in batches, before reads switch |
| Readers | Accept both shapes during the transition | Switch from the array to a join, in a separate deploy |
| Old and new shape together | Yes, in the same collection | Only by keeping two copies (array and table) in sync |
| Deploys | Tolerant readers, then new-shape writers | Dual write, read switch, optionally remove dual write |
| After the migration | Same `find_one` and `$set` | Joins and multi-table transactions from then on |

So the old app is still fine and nobody needs downtime, in either database. The cost of the Postgres side is in the migration itself, and then in living with the child table. The rest of this section is about that second part.

### After the migration: living with the child table

Once the migration is done, the hobbies live in `employee_hobbies` for good. Sections 1 to 4 cover what that means for everyday code: writes, reads, further nesting, and the old app. Sections 5 and 6 cover the objections and concessions.

### 1. Writes: one document vs. a multi-table transaction

**MongoDB.** Saving an employee with a department and hobbies is one statement on one document. Single-document writes are atomic, so no transaction is needed and no reader can ever see a half-written state:

```python
employees.update_one(
    {"emp_no": emp_no},
    {"$set": {"department": department, "hobbies": hobbies}},
)
```

**PostgreSQL.** The same logical write touches two tables. There is no `$set` for a child table, so "replace the list" means delete the old rows and insert the new ones. Done correctly, it looks like this:

```python
with conn:                                    # one transaction
    with conn.cursor() as cur:
        # Lock the parent row so concurrent writers of this employee serialize.
        # The UPDATE below would also lock it, but keep the lock explicit: it
        # must not depend on statement order or on the UPDATE being present.
        cur.execute("SELECT 1 FROM employees WHERE emp_no = %s FOR UPDATE", (emp_no,))
        cur.execute(
            "UPDATE employees SET department = %s WHERE emp_no = %s",
            (department, emp_no),
        )
        cur.execute("DELETE FROM employee_hobbies WHERE emp_no = %s", (emp_no,))
        if hobbies:
            execute_values(
                cur,
                "INSERT INTO employee_hobbies (emp_no, hobby) VALUES %s",
                [(emp_no, h) for h in hobbies],
            )
```

Every line in that block is there for a reason, and each one is easy to get wrong. Three realistic mistakes:

**Mistake (a): no transaction.** Many services run with `autocommit = True` (as `microservice_two.py` does, correctly, for its read-only queries). If the write path does too, a reader that runs between the `DELETE` and the `INSERT` sees the employee with **no hobbies at all**. If the process crashes between the two statements, the hobbies are **gone permanently**.

**Mistake (b): a transaction, but no parent-row lock.** This one surprises people. Under `READ COMMITTED` (the PostgreSQL default), two concurrent "replace the hobbies" transactions can **merge** their results instead of one winning.

It happens whenever nothing locks the employee row *before* the `DELETE`. In the code above, the `UPDATE employees` would take that lock even without the `FOR UPDATE`, because an `UPDATE` locks the rows it changes. That protection is accidental, though, and easy to lose:
- an "edit hobbies" endpoint that only touches `employee_hobbies` and never updates the employee row;
- a refactor that moves the `DELETE` above the `UPDATE`;
- a change that skips the `UPDATE` when the department hasn't changed.

You can reproduce the merge with two `psql` sessions that only touch the hobbies table:

```
-- Start state: employee 10001 has hobbies {movies, cycling}

Session A                                      Session B
---------                                      ---------
BEGIN;
DELETE FROM employee_hobbies
 WHERE emp_no = 10001;      -- deletes 2 rows,
                            -- holds row locks
                                               BEGIN;
                                               DELETE FROM employee_hobbies
                                                WHERE emp_no = 10001;
                                               -- blocks, waiting on A's row locks
INSERT INTO employee_hobbies (emp_no, hobby)
VALUES (10001,'hiking'), (10001,'reading');
COMMIT;
                                               -- unblocks: the 2 old rows are already
                                               -- deleted, and A's new rows were not in
                                               -- B's snapshot, so B deletes 0 rows
                                               INSERT INTO employee_hobbies (emp_no, hobby)
                                               VALUES (10001,'running'), (10001,'singing');
                                               COMMIT;

-- End state: {hiking, reading, running, singing}
-- Neither A's intent nor B's intent: a lost update.
```

The fixes are to lock the parent row as the first statement in the transaction, or to use `REPEATABLE READ`/`SERIALIZABLE` plus retry logic for serialization failures. An explicit `SELECT ... FOR UPDATE`, as shown above, is the clearest way to lock the parent row: it states the intent and doesn't depend on which other statements happen to run, or in what order. Either way, it is concurrency engineering the developer has to know to do. In MongoDB, two concurrent `$set`s on `hobbies` each replace the whole array atomically: last writer wins, and the result is always one of the two intended states.

**Mistake (c): incremental changes create duplicates.** "Add a hobby" in MongoDB is atomic and deduplicating:

```python
employees.update_one({"emp_no": emp_no}, {"$addToSet": {"hobbies": "hiking"}})
employees.update_one({"emp_no": emp_no}, {"$pull":     {"hobbies": "movies"}})
```

The child table in `alter_model_v2.py` has a surrogate `id BIGSERIAL` key and no uniqueness rule, so a retried request or a double-clicked button inserts `hiking` twice. Fixing it is another schema decision plus a different write statement:

```sql
ALTER TABLE employee_hobbies ADD CONSTRAINT uq_emp_hobby UNIQUE (emp_no, hobby);

INSERT INTO employee_hobbies (emp_no, hobby) VALUES (10001, 'hiking')
ON CONFLICT (emp_no, hobby) DO NOTHING;
```

### 2. Reads: reassembling the object

In MongoDB the document *is* the object the app wants:

```python
employees.find_one({"emp_no": 10001})
# {"emp_no": 10001, "first_name": "...", "department": "Sales",
#  "hobbies": ["movies", "cycling", "hiking"]}
```

In PostgreSQL the database returns rows, and a join returns **one row per hobby**:

```sql
SELECT e.emp_no, e.first_name, h.hobby
FROM employees e
LEFT JOIN employee_hobbies h ON h.emp_no = e.emp_no
WHERE e.emp_no IN (10001, 10002);
```
```
 emp_no | first_name | hobby
--------+------------+---------
  10001 | ...        | movies
  10001 | ...        | cycling
  10001 | ...        | hiking
  10002 | ...        | NULL       <- no hobbies, but still a row
```

Something has to fold those rows back into one object. Either SQL does it (`GROUP BY` + `array_agg`, as `microservice_two.py` does) or app code or an ORM does it. ORMs handle this by deduplicating a join or by issuing one extra query per relationship. Either way, it is work and configuration that simply doesn't exist on the MongoDB side.

**Paging trap.** `LIMIT` counts rows, not employees:

```sql
-- Intended: 5 employees. Actual: 5 *rows*, e.g. 2 employees,
-- the second with only some of its hobbies.
SELECT e.emp_no, h.hobby
FROM employees e JOIN employee_hobbies h ON h.emp_no = e.emp_no
WHERE e.department = 'Sales'
LIMIT 5;
```

`microservice_two.py` avoids this only because it does `GROUP BY e.emp_no` *before* `LIMIT 5`. That is correct, but it is one more thing someone has to know.

**The second list is where it really bites.** Suppose the next release adds `skills` the same way (a new `employee_skills` child table). The obvious query joins both:

```sql
SELECT e.emp_no,
       array_agg(h.hobby) AS hobbies,
       array_agg(s.skill) AS skills
FROM employees e
LEFT JOIN employee_hobbies h ON h.emp_no = e.emp_no
LEFT JOIN employee_skills  s ON s.emp_no = e.emp_no
WHERE e.emp_no = 10001
GROUP BY e.emp_no;
```

With 3 hobbies and 4 skills, the two joins produce a **cross product: 3 × 4 = 12 rows** before aggregation. The result is silently wrong:

```
hobbies = {movies,movies,movies,movies,cycling,cycling,cycling,cycling,hiking,hiking,hiking,hiking}
skills  = {python,sql,go,rust,python,sql,go,rust,python,sql,go,rust}
```

`array_agg(DISTINCT ...)` hides the symptom, but it also collapses legitimate duplicates, loses ordering, and still makes the database build the 12 rows. The correct form aggregates each list separately:

```sql
SELECT e.emp_no,
       (SELECT array_agg(h.hobby) FROM employee_hobbies h WHERE h.emp_no = e.emp_no) AS hobbies,
       (SELECT array_agg(s.skill) FROM employee_skills  s WHERE s.emp_no = e.emp_no) AS skills
FROM employees e
WHERE e.emp_no = 10001;
```

In general, joining *n* independent lists of about *k* items each produces about *kⁿ* rows per parent. In MongoDB, adding `skills` changes nothing about the read: `find_one` still returns one document with two arrays.

A smaller detail: an employee with no hobbies comes back as `NULL` from `array_agg`, not `[]`, unless you wrap it in `COALESCE(..., '{}')`. MongoDB returns whatever was stored.

### 3. Nesting: when the list grows again

Requirements rarely stop at change v2. Say the next release also needs the events attended for each hobby.

**MongoDB:** the document just gets deeper. No DDL, and the read is still `find_one`:

```json
{
  "emp_no": 10001,
  "hobbies": [
    { "name": "cycling", "level": "advanced", "since": 2019,
      "events": [ { "name": "City Century", "year": 2025 } ] },
    { "name": "reading", "level": "casual",   "since": 2010, "events": [] }
  ]
}
```
```python
# Query into the nested structure, served by a multikey index
employees.find({"hobbies": {"$elemMatch": {"name": "cycling", "level": "advanced"}}})

# Append an event to one specific hobby, atomically
employees.update_one(
    {"emp_no": 10001},
    {"$push": {"hobbies.$[h].events": {"name": "Gran Fondo", "year": 2026}}},
    array_filters=[{"h.name": "cycling"}],
)
```

**PostgreSQL:** each level of nesting is another table, another FK, and another join. If the events start in some other form, that's another expand-contract migration like the one above:

```sql
CREATE TABLE hobby_events (
    id        BIGSERIAL PRIMARY KEY,
    hobby_id  BIGINT NOT NULL REFERENCES employee_hobbies(id) ON DELETE CASCADE,
    name      VARCHAR(100) NOT NULL,
    year      SMALLINT
);
CREATE INDEX ON hobby_events (hobby_id);
```

Rebuilding the object the app actually wants now takes nested JSON aggregation:

```sql
SELECT e.emp_no,
       COALESCE((
         SELECT json_agg(json_build_object(
                  'name',  h.hobby,
                  'level', h.level,
                  'since', h.since,
                  'events', COALESCE((
                      SELECT json_agg(json_build_object('name', ev.name, 'year', ev.year))
                      FROM hobby_events ev
                      WHERE ev.hobby_id = h.id), '[]'::json)))
         FROM employee_hobbies h
         WHERE h.emp_no = e.emp_no), '[]'::json) AS hobbies
FROM employees e
WHERE e.emp_no = 10001;
```

Writes now span three tables. Also, the "delete and reinsert hobbies" approach from section 1 now cascade-deletes every hobby's events as well, so the write path has to switch to a careful diff-and-update. Note that the SQL above builds a JSON document anyway: at this point the relational model is being used to *emulate* the document model.

### 4. The old app is unaffected, but not fully isolated

The old app needs no code change and no downtime. However, the FK adds database-level rules that now apply to statements the old app already runs, rules its authors never knew about. What happens depends on the FK's delete rule:

| Old-app operation | FK default (`NO ACTION`) | FK `ON DELETE CASCADE` (what `alter_model_v2.py` uses) |
|---|---|---|
| `DELETE FROM employees WHERE emp_no = ...` | **Fails:** `ERROR: update or delete on table "employees" violates foreign key constraint "employee_hobbies_emp_no_fkey"` | Succeeds, and silently deletes the hobbies (usually what you want) |
| "Upsert" implemented as `DELETE` + `INSERT` of the employee row | **Fails**, same error | Succeeds, but **hobbies are permanently lost** while the employee still exists |
| Nightly reload job: `TRUNCATE employees` then bulk load | **Fails:** `ERROR: cannot truncate a table referenced in a foreign key constraint` | **Also fails.** `ON DELETE CASCADE` does not apply to `TRUNCATE`. The job would need `TRUNCATE ... CASCADE`, which wipes all hobbies |

So an "additive" change can break an old batch job outright, or quietly delete new data that the old code never knew existed.

**Be honest about MongoDB here too.** MongoDB old apps are not perfectly isolated either:
- An old app that uses `replace_one` (or an ODM that saves the whole object) **without** the `hobbies` field wipes the hobbies. This is the direct equivalent of the delete-and-reinsert row above.
- An old app with a strict deserializer (for example, a Java POJO codec or a Pydantic model with `extra="forbid"`) can fail when it reads documents that contain an unknown `hobbies` field.

The difference is *where* the hazard lives. In MongoDB, it depends on how the old app writes (`$set` is safe, whole-document replace is not), and that is visible in the old app's own code. In PostgreSQL, it is a database rule added by someone else's migration, and it changes the behavior of old-app statements that did not change.

### 5. The DBA's counter: "why move to a table at all?"

Expect a DBA to ask whether the structured list could stay in the `employees` row, as the flat `text[]` did. The options:

- **Arrays of composite types** (`CREATE TYPE hobby AS (name text, level text, since int)` plus a `hobby[]` column). They exist and keep the list in one row, but updating one element, querying inside them and indexing them are all awkward, and few tools or ORMs support them. Rarely used in practice.
- **JSONB.** This keeps the list in the row and is the closest thing PostgreSQL has to a document, but it gives up typed columns, foreign keys and simple per-field indexes. It's a big enough topic for its own article.
- **Stay with `text[]` and encode the details into strings** (`'cycling:advanced:2019'`). Possible, but every reader and writer has to parse it, and the database can no longer check or index the parts. Not a serious option.

So the child table is a fair choice for the comparison: it's what most relational teams would do for typed, structured list items.

### 6. What MongoDB does *not* win

Concede these before the audience raises them:

- **Uptime.** Neither database needs downtime for either change.
- **Flat lists.** For change v1, a `text[]` column is as good as a MongoDB array: one atomic write, no join, and a GIN index if you filter by hobby.
- **Filtering by hobby.** Both are indexed and perform comparably:
  ```sql
  CREATE INDEX ON employee_hobbies (hobby);
  SELECT e.* FROM employees e
  WHERE EXISTS (SELECT 1 FROM employee_hobbies h
                WHERE h.emp_no = e.emp_no AND h.hobby = 'cycling');
  ```
  ```python
  employees.create_index("hobbies")                 # multikey index
  employees.find({"hobbies": "cycling"})
  ```
- **Referential integrity.** PostgreSQL can enforce "every hobby exists in a `hobbies` catalog table" with an FK. MongoDB needs `$jsonSchema` validation (for example an `enum`) or application logic. This is a real PostgreSQL advantage.

### Summary

| Concern | MongoDB | PostgreSQL |
|---|---|---|
| Flat list of strings (v1) | Array field | `text[]` column (a draw) |
| List items gain fields (v2) | No DDL; readers accept both shapes; convert gradually | Expand-contract: table, dual write, batched copy, read switch, optional `DROP COLUMN` |
| Downtime for the old app | None | None |
| Old app code change | None | None |
| DDL needed for v2 | None | `CREATE TABLE` + FK + `emp_no` index, optionally `DROP COLUMN` later (the `department` index is needed in both) |
| Replace the list atomically | One `$set` | Transaction + parent-row lock + delete/insert |
| Add one item, no duplicates | `$addToSet` | `UNIQUE` constraint + `ON CONFLICT DO NOTHING` |
| Read the whole object | `find_one` | `LEFT JOIN` + `GROUP BY` + `array_agg` (+ `COALESCE`) |
| Second list (`skills`) | Nothing changes | New table, and joins now multiply rows: rewrite reads as subqueries |
| List items nest further | Nested fields, `arrayFilters` | Another table per level, nested `json_agg` |
| Effect on old-app statements | `replace_one` without the field drops it | FK changes `DELETE`/`TRUNCATE` behavior |
| Filter by list value | Multikey index | Index on child table + `EXISTS` (comparable) |
| Constrain values to a catalog | `$jsonSchema` / app logic | FK (advantage: Postgres) |

**One-sentence framing for the PoV:** *a flat list is a draw, but when a list's items grow their own fields, PostgreSQL needs a staged migration to a new table (dual writes, a batched copy, several deploys) and joins and multi-table transactions from then on. In MongoDB, the new shape lives next to the old one in the same document, readers accept both, and you convert at your own pace.*

### How to demo it

- **Don't** try to stall the old app over this. Show `microservice_one.py` running untouched on both sides and say that's expected.
- **Concede change v1.** After `alter_model.py`, both `microservice_two.py` files are a single query on one table or collection. Say it's a draw.
- **Run change v2 live** with `alter_model_v2.py` on both sides. MongoDB's running service starts showing both hobby shapes with no restart. Postgres prints its five migration steps, two of which are app deploys it can't do for you. Then switch Postgres to `microservice_two_v2.py` and show the join.
- **Show the after-migration code** for "save employee with hobbies" side by side (sections 1 and 2). The line count and the number of decisions in the Postgres version make the point on their own.
- **Live moment:** run the two-session lost-update script from section 1 in two `psql` windows. It is short, surprising, and correct PostgreSQL behavior, not a bug.
- **Optional finale:** `alter_model_v2.py --contract` while the v1 Postgres `microservice_two.py` is still running. It fails with `UndefinedColumn`, which shows why the cleanup has to wait for every old deploy.
- Lead the overall story with the process cost (Difference 3 in the blog post) and use this section as its concrete, code-level illustration.

## Sparse data and deploy order: the details

`alter_model.py` only fills in `NUM_SAMPLING` (1,000) of the 10,000 employees, on both sides.

- **Querying half-filled data is *not* the problem.** Don't claim it is. `microservice_two.py`'s `distinct("department")` maps cleanly to:
  ```sql
  SELECT DISTINCT department FROM employees WHERE department IS NOT NULL;
  ```
  Both sides need the same filter. SQL's `DISTINCT` returns `NULL` as a value unless you filter it out. MongoDB's `distinct` skips documents that lack the field when it scans the collection, but once an index on the field exists it reads the index, which stores `null` for those documents, so it returns `null` too. `mongodb/microservice_two.py` filters with `{"department": {"$type": "string"}}`.
- **Deploy order is a coordination cost, not a breakage.** Before the schema change, the MongoDB query returns `[]`. The PostgreSQL query fails with `UndefinedColumn`, because the column doesn't exist yet; `postgres/microservice_two.py` prints exactly this when started too early. Concede that both can be handled gracefully: catch the error, use a feature flag, or let a migration tool such as Flyway run the change when the app starts, which settles the order for you. The cost grows in the two situations below, which are the answer when a DBA says "we just catch the error".

### With an ORM, the new field breaks every query on the entity

Hand-written SQL only touches the columns it names, so a missing `department` column breaks only the queries that mention it. Most applications use an ORM instead, which maps a class to the table:

```java
@Entity
@Table(name = "employees")
public class Employee {
    @Id Integer empNo;
    String firstName;
    String lastName;
    String department;      // new field, added for the new feature
}
```

By default the ORM selects **every mapped column** whenever it loads an `Employee`:

```sql
-- what Hibernate runs for employeeRepository.findById(10001), even on the login page:
SELECT e.emp_no, e.first_name, e.last_name, e.department FROM employees e WHERE e.emp_no = ?
```

Deploy this before the `ALTER TABLE` has run, and **every query that loads an employee fails** with `ERROR: column e.department does not exist`: login, payroll and search, not just the new report. The generated `INSERT` fails the same way. With `spring.jpa.hibernate.ddl-auto=validate`, the app won't even start. Django, SQLAlchemy, Entity Framework and ActiveRecord behave similarly by default.

It works in reverse for removals. If you clean up, step 5 of the v2 migration drops the `hobbies` column, and any running version that still maps that field fails on every employee query from then on. So the drop must wait until no deployed version maps it, including any version you might roll back to.

In MongoDB, a document without `department` loads with that field empty (`null`, `undefined` or a default), and every other query keeps working. The caveat, as in section 4: a mapping layer configured to reject unknown fields, such as a Pydantic model with `extra="forbid"`, can fail on a field it doesn't know. That's a choice in the application's code, not a database rule.

### Several services sharing one database

In many companies the `employees` table isn't private to one service: HR, Payroll, Reporting and a nightly ETL job may all read it, each owned by a different team with its own release schedule. Say Payroll needs `department` and is ready on Tuesday, but the schema belongs to HR, whose next release, with the migration in it, is on Thursday. If Payroll ships first, Payroll breaks until Thursday, and with an ORM all of Payroll breaks.

So someone has to own the migration, and the order is strict:
- **The schema change ships first, on its own,** and must be backward compatible: adding a column is fine, renaming or dropping one isn't.
- **Consumers then adopt it** on their own schedules.
- **Removing anything waits until every consumer has stopped using it.** That means knowing all of them: services, reports, ETL jobs and analysts' saved queries. That's often the hardest part in practice.
- **Only one place runs migrations.** Two services each running Flyway against the same schema compete over its migration history, so teams funnel schema changes through one owner, who becomes a bottleneck for everyone else.

With MongoDB there's no schema step to own: Payroll writes and reads `department` when it's ready, and the other consumers are unaffected by a field they don't use. Teams still have to agree on the *shape* of shared data. If hobbies change from strings to objects, every consumer must accept both shapes. But that agreement lives in application code and rolls out gradually, with no single database change that has to land before anyone can ship.

## Locks: the details for the DBA conversation

The blog post shows the lock queue and the `lock_timeout` fix. A DBA will also expect you to get these right:

- **`lock_timeout` + retry fits in one script.** For this proof's change (nullable columns and a new table), the mitigation is a retry loop inside a single migration script, run once. Multi-deploy expand-contract is needed for renames, type changes, `NOT NULL`, or splitting columns. Don't claim it's needed here.
- **`CREATE INDEX` blocks writes.** A plain `CREATE INDEX` (as `alter_model.py` uses for `department`) blocks all writes to the table for the entire build. In production you'd use `CREATE INDEX CONCURRENTLY`, which can't run inside a transaction and can fail, leaving an `INVALID` index that must be dropped and rebuilt.
- **Foreign keys lock the parent table.** Creating `employee_hobbies` with `REFERENCES employees` (change v2, step 1) takes a `SHARE ROW EXCLUSIVE` lock on `employees`, which blocks writes (not reads) for its duration and queues like any other lock. Use `lock_timeout` here too. On an existing large child table, the usual pattern is `ADD CONSTRAINT ... NOT VALID` followed by a separate `VALIDATE CONSTRAINT`.
- **MongoDB index builds lock too, briefly.** Both `alter_model.py` scripts create the same `department` index, so compare the builds fairly: since MongoDB 4.2, the default index build takes an exclusive lock on the collection only briefly at the start and end, and allows reads and writes in between. That's closer to `CREATE INDEX CONCURRENTLY` than to a plain `CREATE INDEX`, without the opt-in.
- **Any long-running transaction blocks DDL, not just forgotten ones.** A reporting query, a batch job or a `pg_dump` holds a lock on the table for as long as it runs, and the `ALTER` queues behind it like it would behind an idle session. The demo uses an idle transaction only because it's the easiest to reproduce on stage.
- **`idle_in_transaction_session_timeout` helps, but only with idle sessions.** Set it server-wide so a forgotten commit can't hold a table for long. It doesn't touch a query that's actively running, so it complements `lock_timeout` and retry rather than replacing them.
- **Check for long-open transactions first.** Before running DDL on a busy table, look for open transactions, active or idle:
  ```sql
  SELECT pid, state, now() - xact_start AS open_for, left(query, 50) AS query
  FROM pg_stat_activity
  WHERE xact_start IS NOT NULL AND pid <> pg_backend_pid()
  ORDER BY open_for DESC;
  ```

**The honest framing:** the cost is the operational knowledge needed to make "safe" DDL actually safe on a live table, not extra deploys. Adding fields in MongoDB involves no DDL and no table lock, so there is nothing to get wrong.
