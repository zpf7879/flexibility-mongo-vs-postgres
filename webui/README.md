# Demo web UI

A browser page for presenting the MongoDB vs. PostgreSQL demo from the [main README](../README.md), instead of juggling several terminals, `psql` and Compass.

Behind the scenes it runs the same scripts in `../mongodb` and `../postgres`, unchanged, and streams their output to the page. Everything the walkthrough asks you to do in a terminal has a button here, and the page warns you before the usual mistakes.

## Requirements

- **Python 3.8 or later.** The server uses only the standard library, plus `pymongo` and `psycopg2` from the project's `requirements.txt`.
- **A MongoDB Atlas cluster** (M10 or larger), and a database user that can read and write any database.
- **Docker**, to run PostgreSQL 16 from the project's `docker-compose.yml`.

Install the drivers once, from the project directory:

```bash
pip install -r requirements.txt
```

If you use a virtual environment, install into it and start the server with that environment's Python: the page runs the demo scripts with the same Python as the server.

## Start it

From the project directory:

```bash
python webui/server.py
```

The page opens at <http://127.0.0.1:8765>. Options:

| Option | What it does |
|---|---|
| `--port 9000` | Use another port |
| `--no-browser` | Don't open a browser tab |

Press **Ctrl+C** in that terminal to stop the server. It stops every script it started, too.

Run only one copy at a time. If you update the code, restart the server to pick up the changes.

## Before the audience arrives: Step 0

Step 0, **Setup and pre-flight**, gets both databases ready. Both status chips in the top bar should turn green.

### MongoDB

The **MongoDB connection** dialog opens by itself the first time. You can also open it from the top bar.

1. **Hostname:** the cluster hostname from Atlas, such as `cluster0.abcde.mongodb.net`. You'll find it under **Connect → Drivers**: it's the part after `@` in the connection string. Pasting the whole connection string works too: only the hostname is kept.
2. **Username** and **Password:** the *database user* from **Database Access** in Atlas, not your Atlas login. Special characters in the password are fine.
3. Click **Test connection**. On success it shows the MongoDB version and the user it logged in as.
4. Click **Save**.

The details are kept in the server's memory only, never written to a file, and passed to the MongoDB scripts as `MONGODB_URI`. To skip the dialog, set `MONGODB_URI` before starting the server. The dialog then opens pre-filled from it.

An Atlas hostname connects with `mongodb+srv://`. A hostname with a port, such as `localhost:27017`, connects with `mongodb://`.

If the test fails, it says what to check:

| The test says | What to do |
|---|---|
| The cluster was found, but none of its servers answered | Add your current IP address in Atlas under **Security → Network Access → Add current IP address**. If it's already there, a firewall or VPN may be blocking port 27017: try another network. |
| The TLS handshake failed | Usually the same IP access list problem. Otherwise a proxy or antivirus may be intercepting TLS. |
| Wrong username or password | Check the database user under **Database Access** in Atlas. |
| Hostname not found | Copy the hostname again from **Connect → Drivers**. |
| Connection refused | Nothing is listening on that host and port. |

### PostgreSQL

Click **Run** next to **Start PostgreSQL** (`docker compose up -d`). The first start takes a few seconds before Postgres accepts connections. **Show the container status** runs `docker compose ps`. Docker's output appears in the **docker compose** panel at the bottom of the page.

The page connects to Postgres with the settings in `../postgres/demo_settings.py`: `localhost:5432`, database `flexible_rdbms`.

### Starting clean

If you've run the demo before, click **Reset…** in the top bar first.

## Running the demo

The step list on the left follows the walkthrough in the [main README](../README.md#try-it-yourself-running-the-demo):

| Step | What you run |
|---|---|
| 0. Setup and pre-flight | Connect MongoDB, start PostgreSQL |
| 1. Load the data | `create_model.py` in both |
| 2. Start the old service | `microservice_one.py` in both, left running |
| 3. Deploy the new service early | `microservice_two.py` in both: Postgres fails on purpose, MongoDB waits |
| 4. Apply change v1 | `alter_model.py` in both |
| 5. Check the old service | Nothing to run: point at the `microservice_one.py` panels |
| 6. Run the new service on change v1 | `microservice_two.py` in Postgres |
| 7. Change v2: hobbies grow up | `alter_model_v2.py` in both, then **Switch reads**, then the optional finale |
| 8. Show the lock queue | `lock_contention_demo.py`, with the lock monitor open |
| 9. Close with the process | Nothing to run: questions for the audience |
| ✓ Clean up | **Stop every running script**, **Reset…** |

Each step shows its talking points and buttons for **MongoDB**, **PostgreSQL** or **Both**. **Next step →** moves on, and the page remembers the step you're on if you refresh it.

### The output panels

The two columns show MongoDB on the left and PostgreSQL on the right. Under each column title, a status line shows the state of the data right now, for example how many employees have the v1 fields, or whether hobbies are in a `text[]` column or a child table.

Each column has:

- **scripts:** one-off scripts, such as `create_model.py` or `alter_model.py`, run here one at a time.
- **One panel per microservice**, with **Start** and **Stop** buttons and a running/stopped badge. Services keep running across steps, as in the README walkthrough.

**Clear** empties a panel. The panels that matter for the current step are outlined.

### Warnings

Before running a script, the page checks the state of the data and asks first if something looks wrong, for example:

- running change v2 before change v1,
- reloading the data while services are still running,
- running `--contract` while the old Postgres `microservice_two.py` still reads the column it drops.

Failures that are part of the demo, like PostgreSQL's `microservice_two.py` in Step 3, run without a warning. **Run anyway** always lets you go ahead.

### Step 7: switching reads

**Switch reads** stops PostgreSQL's `microservice_two.py` and starts `microservice_two_v2.py`, which reads from the child table. It's step 4 of the rollout in the README's [Difference 3](../README.md#difference-3-the-rollout-and-the-process-around-it).

### Step 8: the lock monitor

The **Lock monitor** replaces the `psql` window. It shows `pg_stat_activity` every second: who is blocked, and by which pid. Blocked sessions are red, and idle-in-transaction sessions are amber. It opens by itself on Step 8, and **Lock monitor** in the top bar toggles it at any time.

The page's own database connections are left out of the monitor, and they give up quickly if a table is locked, so the page never joins the queue it's showing.

### Data peek

**Data peek**, under each column, replaces Compass and `psql`. It shows sample documents or rows in each shape the data can be in right now:

- **MongoDB:** a document without the v1 fields, one with hobbies as strings, and one with hobbies as objects.
- **PostgreSQL:** rows with and without the v1 columns (`NULL`s shown), and one employee's rows in `employee_hobbies`.

While it's open, it refreshes each time a script on that database finishes. **Refresh** reloads it by hand.

## Cleaning up

**Reset…** stops every running script and runs `clean_environment.py` in both databases. Tick the box to also run `docker compose down -v`, which removes the Postgres container and its data.

**Stop all** in the top bar only stops the running scripts.

## Troubleshooting

**The page doesn't change after an update, or a new button returns errors.** An older copy of the server is still running. Check every terminal, including WSL. With WSL, a server started inside Linux also answers on `127.0.0.1:8765` in Windows, so stop it there or use `--port`.

**PostgreSQL won't start from the page.** Look at the **docker compose** panel. If it mentions a remote host, your Docker context points somewhere else: check with `docker context ls`.

**The MongoDB status line shows an error.** Open **MongoDB connection** and click **Test connection**: it explains the cause, as in the table [above](#mongodb).

## Security

- The server only listens on `127.0.0.1`, and refuses requests addressed to any other host name.
- The page can only run the scripts and arguments listed in `server.py`, and only `docker compose up -d`, `ps` and `down -v`.
- MongoDB credentials stay in the server's memory and are never sent back to the page.

## Files

```
webui/
├── README.md         this file
├── server.py         the server: runs the scripts, checks the databases
└── static/
    ├── index.html    the page
    ├── app.js        steps, warnings, live output, lock monitor, data peek
    └── app.css       styles, with dark mode
```
