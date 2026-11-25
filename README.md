# Salesforce Data Migration and Integration

## Purpose and Progress

I created this project in response to Gallagher's advertised [Data Engineer](https://careers.gallagher.com/jobs/8676662-data-engineer) opportunity. The goal is to move customer and licence data into Salesforce and check that it arrived correctly.

The customer migration works today. Preparation includes the [product and licence models](docs/licence-model.md), validated in a live Databricks run. Salesforce loading still covers customer Accounts only. See the [glossary](GLOSSARY.md) for the business terms.

Python reads fictional customers from Databricks in batches, validates them, and prepares Salesforce accounts. By default it previews the original customer source without writing to either system. With `--load`, dbt creates and tests customer, product and licence snapshots, including their relationships. Python reads the tested customer table, saves each valid Account and checks all five mapped fields by readback. A live run passed all six SQL unit tests and 24 data checks, built all three snapshots, and updated and verified all 20 customer Accounts. Large-scale performance has not been tested.

The checks require non-empty text for customer IDs, names, cities, postcodes and country codes. Country codes must have two uppercase letters; NZ postcodes must have [four digits, including leading zeros](https://www.nzpost.co.nz/personal/sending-in-nz/postcodes). These are format checks, not address verification. A full load also checks that source IDs are present and unique before sending records.

## Preparing Salesforce Accounts

| Customer field | Salesforce Account field |
| --- | --- |
| `customer_id` | `Source_Customer_ID__c` (custom) |
| `legal_name` | `Name` |
| `billing_city` | `BillingCity` |
| `billing_postal_code` | `BillingPostalCode` |
| `billing_country_code` | `BillingCountryCode` |

Create `Source_Customer_ID__c` on Account as Text (36), marked **External ID** and **Unique**. The existing UUID lets later runs [match and update the same account](https://developer.salesforce.com/docs/platform/api-rest/guide/dome-upsert.html). Enable [State and Country/Territory Picklists](https://help.salesforce.com/s/articleView?id=000220089&language=en_US&type=1), including NZ. A read-only check confirmed these settings and field permissions in the demo account.

## Run Locally

You need Python 3.13 and a Databricks workspace with a SQL warehouse. Run the commands below from this repository's folder on macOS or Linux.

### 1. Upload the sample data

Upload [customers_v1.csv](p0_data/seed/customers_v1.csv) to Databricks. It contains fictional organisations and contact details, not Gallagher customer data.

Use the first row as column names, a comma separator and UTF-8 encoding. Set `billing_postal_code` to `STRING` so values such as `0110` keep their leading zero. `source_revision` can be `BIGINT`. Keep the customer IDs unchanged between uploads.

For `--load`, also upload [products_v1.csv](p0_data/seed/products_v1.csv) and [licences_v1.csv](p0_data/seed/licences_v1.csv) alongside the customer table, named `products_v1` and `licences_v1`. Use `STRING` for IDs, codes and names, `BOOLEAN` for `is_active`, `BIGINT` for seats, and `DATE` for licence dates. Blank cancellation dates must be null. Preview needs only the customer source.

### 2. Install and configure

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp -n .env.example .env
```

This creates a separate Python environment and copies the example settings without replacing an existing `.env` file.

Fill in the Databricks settings in `.env`. Salesforce settings are needed for loading and recovery:

| Setting | What to enter |
| --- | --- |
| `DATABRICKS_SERVER_HOSTNAME` | Warehouse hostname, without `https://` |
| `DATABRICKS_HTTP_PATH` | Warehouse HTTP path |
| `DATABRICKS_TOKEN` | Personal access token; loading also needs permission to create the dbt schema and tables |
| `DATABRICKS_CUSTOMERS_TABLE` | Original source table in `catalog.schema.table` form, such as `workspace.default.customers_v1` |
| `SALESFORCE_MY_DOMAIN_URL` | My Domain URL, including `https://`, without a page path |
| `SALESFORCE_CLIENT_ID` | External Client App consumer key |
| `SALESFORCE_CLIENT_SECRET` | External Client App consumer secret |

Find the hostname and HTTP path under **SQL Warehouses → your warehouse → Connection Details**. The workspace ID and token ID entries are unused and can stay blank. Git ignores `.env` and `.venv`; keep credentials in `.env`.

For Salesforce, configure an [External Client App with Client Credentials Flow](https://help.salesforce.com/s/articleView?id=sf.configure_client_credentials_flow_for_external_client_apps.htm&language=en_US), the `api` scope, and a Run As user with access to Account and the mapped fields.

### 3. Preview the customers

```bash
python main.py
```

Preview reads the original table and runs the Python checks. It does not run dbt or apply its SQL transformations, so its results can differ from a load. With the sample CSV uploaded, expect:

```text
Batch 1: 6 customers

Batch 2: 6 customers

Batch 3: 6 customers

Batch 4: 2 customers

Summary
------------------------------------------------------------
Total customers retrieved: 20
Valid customers: 20
Rejected customers: 0
Accounts prepared for Salesforce: 20
```

### 4. Load and verify the customers

This command **builds and tests three Databricks snapshot tables, then creates or updates customer Accounts in Salesforce**. If any dbt check fails, no Salesforce requests are made:

```bash
python main.py --load
```

Each batch is prepared, saved and checked before the next batch is fetched. Salesforce requests currently run one account at a time, using one login for the run. The final report counts created, updated and verified accounts.

Salesforce requests make at most three attempts. Reads retry timeouts, broken connections and HTTP 429, 500, 502, 503 or 504, normally waiting one then two seconds. A valid `Retry-After` header can extend each wait to 30 seconds; a longer requested wait stops the run. Other HTTP errors, including 401 and 403, and certificate errors are not retried.

Authentication and saves retry only a connection timeout that [Requests identifies as safe to retry](https://requests.readthedocs.io/en/latest/_modules/requests/exceptions/#ConnectTimeout). A lost save response leaves the outcome uncertain, so the run stops for inspection. Retrying a verification read never repeats the save. The console identifies each retry without printing credentials or raw error details.

With all 20 Accounts already present, a successful rerun ends with:

```text
Summary
------------------------------------------------------------
Total customers retrieved: 20
Valid customers: 20
Rejected customers: 0
Accounts prepared for Salesforce: 20
Accounts created: 0
Accounts updated: 20
Accounts verified: 20

Run outcome: complete
```

Console headings separate preparation, migration and the summary. Each load saves a report in `reports/` and prints its path. The `.jsonl` file contains one JSON record per line: the run start, rejected customers and reasons, account load attempts, verified accounts, and a final summary. Git ignores these local reports.

Before each Salesforce save, an `account_load_started` entry is written, flushed and synced to disk. After readback confirms the fields, an `account_verified` entry is saved the same way. If the first entry has no matching verification, the account needs inspection: the save may or may not have reached Salesforce. A report-storage error stops processing before another save.

| Load outcome | Meaning | Exit status |
| --- | --- | --- |
| `complete` | Every retrieved customer was verified; an empty source completes with zero counts. | 0 |
| `incomplete` | Processing finished, but some customers were rejected. Valid customers were still loaded. | 1 |
| `failed` | An error or interruption stopped processing. Earlier writes may remain in Salesforce. | Nonzero |

Preview reports validation problems without saving a report or failing because of rejected records. A failed dbt check stops before migration, so no migration report is created. A report without a final summary does not establish success. Use the recovery commands below to inspect or resume an interrupted load. Older reports without load-attempt entries cannot identify a save interrupted before verification.

Each load creates three new Delta tables and records their full names in the report. Relationship checks use these snapshots; Python's customer ID check and extraction use the tested customer snapshot. Later source changes or loads do not replace them. The loader uses Salesforce API v68.0 and updates only the mapped customer fields. Product and licence loading is not implemented yet.

## How the Code Fits Together

[main.py](main.py) coordinates two phases:

1. [prepare_migration()](p1_preparation/prepare.py) returns a `MigrationTables` value containing the customer source for preview, or all three tested snapshot names for loading.
2. [migrate_customers()](p2_migration/migrate.py) reads the selected customer table in batches, validates and maps the rows, and optionally saves and verifies Salesforce Accounts. Load reports retain all three snapshot names.

The numbered folders make the flow easy to follow: `p0_data/` holds the sample CSV, `p1_preparation/` owns dbt, and `p2_migration/` contains the Databricks reader, Salesforce requests, mapping and validation. `main.py` controls execution. Each phase explains its expected failures; unexpected programming errors retain their traceback for debugging.

[stage_customers.sql](p1_preparation/dbt/models/stage_customers.sql) trims surrounding spaces, capitalises country codes and turns empty values into nulls. IDs stay unchanged. Seven dbt data checks cover missing values and duplicate or blank IDs; Python also checks country and postcode formats.

The dbt model is still called `stage_customers`. Its database table is named `workspace.migration.customers_snapshot_<unique_id>`; dbt calls this alternate name an **alias**. Python supplies a fresh name for every load. The full name appears in the terminal and in the migration report's first record. Both phases use the existing `.env` settings.

Snapshots remain in Databricks for inspection, including after a failed data check. Do not edit or replace them. Retaining them uses storage; automatic cleanup is not implemented. A new `--load` creates fresh snapshots; `--resume` continues customer loading from the existing customer snapshot. These are stored copies for one load, not dbt's separate change-history feature called `snapshot`. Original source tables and any old `stage_customers` view are left untouched.

## Check an Interrupted Load

Use the report path printed by the load, replacing `YOUR_RUN_ID` below:

```bash
python main.py --reconcile reports/YOUR_RUN_ID.jsonl
```

This reads the report to find an account load attempt without recorded verification. It reads that customer from the saved snapshot and compares the five mapped fields with Salesforce. It does not run dbt, save accounts, change the report or resume loading. `--reconcile` cannot be combined with `--load`.

Matching fields mean the account matches **now**, not that a particular request succeeded or its automation ran only once. Differences, an unavailable account or a failed check produce a nonzero exit. With no recorded uncertain attempt, the command exits successfully without contacting either service; this does not establish that the migration finished or recheck previously verified accounts.

New reports record `DATABRICKS_SERVER_HOSTNAME` and `SALESFORCE_MY_DOMAIN_URL`, without tokens or client credentials. Before checking an uncertain account, reconciliation requires those addresses to match the current settings exactly. This checks configured addresses, not permanent workspace or organisation IDs. The snapshot must still exist unchanged. Older reports missing the addresses or matching attempt entries, and malformed reports, require manual inspection. A [Salesforce lookup returning HTTP 404](https://developer.salesforce.com/docs/platform/api-rest/guide/resources-sobject-upsert-get.html) stays unresolved rather than being treated as permission to create an account.

## Resume an Interrupted Load

This command **can save accounts**. Use the original report, with the same connection addresses, unchanged snapshot and field mapping:

```bash
python main.py --resume reports/YOUR_RUN_ID.jsonl
```

Resume skips previously verified accounts, checks any uncertain account by readback, then loads unattempted customers. A differing or unavailable uncertain account stops the resume before further saves. Rejected customers are checked again and keep the outcome incomplete if still invalid. A completed run does nothing. `--load`, `--reconcile` and `--resume` are separate modes.

Progress is appended to the same report. `resume_started` marks each attempt; `account_reconciled` records matching fields without claiming another create or update. If resume is interrupted, use the same command and report again. Keep the report intact; truncated or contradictory history requires manual inspection.

The final verified total includes previous verifications, reconciled accounts and successful saves. Created, updated and rejected counts describe the latest attempt. Previously verified accounts are not read from Salesforce again, so later edits there are not detected.

Resume scans the retained snapshot in batches and checks that previously verified IDs still exist before proceeding. It retains verified IDs in memory and adds a snapshot scan when earlier progress exists; large-scale recovery performance has not been tested. It relies on the snapshot remaining unchanged and does not provide exactly-once Salesforce automation.

A local [file lock](https://docs.python.org/3.13/library/fcntl.html#fcntl.flock) prevents simultaneous loading or resuming of the same report on macOS/Linux. It does not coordinate separate reports or copies across machines. Run one migration at a time against the destination.

## Tests

See [what the tests protect](tests/README.md) for their responsibilities, intentional overlap and limits.

```bash
PYTHON_DOTENV_DISABLED=1 python -m unittest discover -s tests -v
```

The tests cover extraction, validation, mapping, Salesforce requests and the batch workflow, including run outcomes and saved reports. They use sample records, simulated responses and temporary report files. `PYTHON_DOTENV_DISABLED=1` prevents dbt from automatically reading a local `.env` file during these offline checks.

[SQL unit tests](p1_preparation/dbt/models/stage_customers_unit_tests.yml) supply example input and expected output. Customer tests cover trimming, country-code capitalisation, leading zeros, unchanged IDs, and blanks or nulls. Product tests cover clean labels and retained inactive products; the licence test preserves historical, future and cancelled terms. These examples substitute for source rows and do not alter the uploaded tables. All six unit tests run during `python main.py --load`; a failure stops preparation before Salesforce loading.

[GitHub Actions](.github/workflows/tests.yml) runs the Python tests and parses the dbt configuration on each push and pull request, using Python 3.13 on Linux. It supplies placeholder snapshot names without creating tables. Parsing checks definitions, not SQL behaviour; the six SQL unit tests and 24 data checks across all three models require Databricks execution.
