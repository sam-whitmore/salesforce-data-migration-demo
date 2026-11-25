# Salesforce Data Migration and Integration

## Purpose and Progress

I created this project in response to Gallagher's advertised [Data Engineer](https://careers.gallagher.com/jobs/8676662-data-engineer) opportunity. The goal is to move customer and licence data into Salesforce and check that it arrived correctly.

Python reads fictional customers from Databricks in batches, validates them, and prepares Salesforce accounts. By default it previews the original source without writing to either system. With `--load`, dbt first creates and tests a separate customer snapshot table. Python reads that same table, saves each valid account and reads it back to check all five mapped fields. The complete dbt-to-Salesforce workflow has updated and verified all 20 sample customers. Large-scale performance has not been tested.

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

### 2. Install and configure

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp -n .env.example .env
```

This creates a separate Python environment and copies the example settings without replacing an existing `.env` file.

Fill in the Databricks settings in `.env`. The Salesforce settings are needed only when loading records:

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

This command **builds and tests a Databricks snapshot table, then creates or updates Salesforce Accounts**. If dbt fails, no Salesforce requests are made:

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

Preview reports validation problems without saving a report or failing because of rejected records. A failed dbt check stops before migration, so no migration report is created. A report without a final summary does not establish success. Inspect uncertain saves before rerunning with the same source IDs; automatic resume is not implemented. Older reports without load-attempt entries cannot identify a save interrupted before verification.

Each load creates a new Delta table containing the prepared rows. dbt's data checks, Python's ID check and batch extraction all read that table. Later source changes or subsequent loads do not replace those rows. The loader uses Salesforce API v68.0 and updates only the mapped fields.

## How the Code Fits Together

[main.py](main.py) coordinates two phases:

1. [prepare_customers()](p1_preparation/prepare.py) selects the original source for preview. With `--load`, it chooses a unique snapshot name, builds and tests that table, and returns its full name.
2. [migrate_customers()](p2_migration/migrate.py) reads that source in batches, validates and maps the customers, and optionally saves and verifies their Salesforce accounts.

The numbered folders make the flow easy to follow: `p0_data/` holds the sample CSV, `p1_preparation/` owns dbt, and `p2_migration/` contains the Databricks reader, Salesforce requests, mapping and validation. `main.py` controls execution. Each phase explains its expected failures; unexpected programming errors retain their traceback for debugging.

[stage_customers.sql](p1_preparation/dbt/models/stage_customers.sql) trims surrounding spaces, capitalises country codes and turns empty values into nulls. IDs stay unchanged. Seven dbt data checks cover missing values and duplicate or blank IDs; Python also checks country and postcode formats.

The dbt model is still called `stage_customers`. Its database table is named `workspace.migration.customers_snapshot_<unique_id>`; dbt calls this alternate name an **alias**. Python supplies a fresh name for every load. The full name appears in the terminal and in the migration report's first record. Both phases use the existing `.env` settings.

Snapshots remain in Databricks for inspection, including after a failed data check. Do not edit or replace them. Retaining them uses storage; automatic cleanup and resuming from an existing snapshot are not implemented yet. A rerun creates a fresh snapshot. This is a stored copy for one load, not dbt's separate change-history feature called `snapshot`. The original source table and any old `stage_customers` view are left untouched.

## Tests

See [what the tests protect](tests/README.md) for their responsibilities, intentional overlap and limits.

```bash
PYTHON_DOTENV_DISABLED=1 python -m unittest discover -s tests -v
```

The tests cover extraction, validation, mapping, Salesforce requests and the batch workflow, including run outcomes and saved reports. They use sample records, simulated responses and temporary report files. `PYTHON_DOTENV_DISABLED=1` prevents dbt from automatically reading a local `.env` file during these offline checks.

[SQL unit tests](p1_preparation/dbt/models/stage_customers_unit_tests.yml) supply example input and expected output for the customer model. They check trimming, country-code capitalisation, leading zeros, unchanged IDs, and blank or null fields. These examples substitute for the source rows during the tests; they are not uploaded into the customer table. The three tests run on Databricks before the snapshot is built during `python main.py --load`. A failure stops preparation before Salesforce loading.

[GitHub Actions](.github/workflows/tests.yml) runs the Python tests and parses the dbt configuration on each push and pull request, using Python 3.13 on Linux. It supplies a placeholder snapshot name for parsing, without creating a table. Parsing checks the SQL unit-test definitions but does not execute them; their results and the seven data-check results require a Databricks run.
