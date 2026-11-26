# Salesforce Data Migration and Integration

## Purpose and Progress

I created this project in response to Gallagher's advertised [Data Engineer](https://careers.gallagher.com/jobs/8676662-data-engineer) opportunity. The goal is to move customer and licence data into Salesforce and check that it arrived correctly.

Live runs have verified 20 customer Accounts and three Products, including repeat updates without new records. Licence loading is implemented and tested offline; live verification is next. See the [product and licence model](docs/licence-model.md) for the relationships and date rules. See the [glossary](GLOSSARY.md) for the business terms.

Python reads fictional customers from Databricks in batches, validates them, and prepares Salesforce accounts. By default it previews the original customer source without writing to either system. With `--load`, dbt creates and tests customer, product and licence snapshots, including their relationships. Python loads customers, products and licences from their tested tables, checking every mapped field by readback. Each licence is linked to its existing Salesforce customer and product. Inactive products are included because existing licences may still refer to them. A live run passed all six SQL unit tests and 24 data checks, built all three snapshots, and updated and verified all 20 customer Accounts. Large-scale performance has not been tested.

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

Create the [Product and Licence fields](docs/licence-model.md#salesforce-destination) and give the integration user access to them. Every new `--load` run checks all three destination models during preparation.

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

### 4. Load and verify the records

This command **builds and tests three Databricks snapshot tables, checks their field mappings against Salesforce, then creates or updates Accounts, Products and Licences**. A failed dbt check prevents Salesforce access; a failed model check prevents Salesforce writes:

```bash
python main.py --load
```

Preparation prints each snapshot column and its actual type beside its intended Salesforce field and type. It checks both schemas and reported permissions, including the Product lookup limitation. All three mappings are checked before loading starts. These checks do not establish record-level access or successful writes.

Each batch is prepared, saved and checked before the next batch is fetched. Salesforce requests run one record at a time, with a login for each stage. The final report counts created, updated and verified Accounts, Products and Licences separately. The run is complete only when all three stages finish without rejected records. Rejected customers or products prevent licence loading.

Salesforce requests make at most three attempts. Reads retry timeouts, broken connections and HTTP 429, 500, 502, 503 or 504, normally waiting one then two seconds. A valid `Retry-After` header can extend each wait to 30 seconds; a longer requested wait stops the run. Other HTTP errors, including 401 and 403, and certificate errors are not retried.

Authentication and saves retry only a connection timeout that [Requests identifies as safe to retry](https://requests.readthedocs.io/en/latest/_modules/requests/exceptions/#ConnectTimeout). A lost save response leaves the outcome uncertain, so the run stops for inspection. Retrying a verification read never repeats the save. The console identifies each retry without printing credentials or raw error details.

With all sample records already present, a successful rerun should end with:

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
Products retrieved: 3
Products rejected: 0
Products created: 0
Products updated: 3
Products verified: 3
Licences retrieved: 12
Licences rejected: 0
Licences created: 0
Licences updated: 12
Licences verified: 12

Run outcome: complete
```

Console headings follow the code: build and test snapshots, compare models, migrate, then summarise. dbt shows progress every five completed checks and a results table, while warnings and errors remain visible. Detailed dbt output stays in `p1_preparation/dbt/logs/dbt.log`. Model comparisons use bordered tables; each verified record gets one line with its name.

Each load saves a report in `reports/` and prints its path. The `.jsonl` file retains the record IDs, run scope, rejections and reasons, load attempts, verified records, and final summary. Git ignores these local reports. If a load stops with an unverified record, the console also prints its source ID.

Before each Salesforce save, an `account_load_started`, `product_load_started` or `licence_load_started` entry is written, flushed and synced to disk. After readback confirms the fields, the corresponding `*_verified` entry is saved the same way. If the first entry has no matching verification, the record needs inspection: the save may or may not have reached Salesforce. A report-storage error stops processing before another save.

| Load outcome | Meaning | Exit status |
| --- | --- | --- |
| `complete` | Every retrieved record in the run’s scope was verified; an empty source completes with zero counts. | 0 |
| `incomplete` | Some records were rejected. Valid rows in those stages were loaded; rejected parents prevent licence loading. | 1 |
| `failed` | An error or interruption stopped processing. Earlier writes may remain in Salesforce. | Nonzero |

Preview reports validation problems without saving a report or failing because of rejected records. A failed preparation check stops before migration, so no migration report is created. Snapshots already built by dbt remain in Databricks. A report without a final summary does not establish success. Use the recovery commands below to inspect or resume an interrupted load. Older reports without load-attempt entries cannot identify a save interrupted before verification.

Each load creates three new Delta tables and records their full names in the report. Relationship checks use these snapshots; Python's ID checks and extraction use those tested tables. Later source changes or loads do not replace them. The loader uses Salesforce API v68.0 and updates only the mapped fields.

Licences use their own source ID to match existing Salesforce records. For each valid term, Python looks up the customer and product by source ID and puts their Salesforce IDs in `Customer__c` and `Product__c`. Missing or inaccessible parents stop the load; no placeholder parent is created. Readback checks both links, seats, all three dates and the licence source ID. Past, future and cancelled terms are included. These sequential lookups favour clear behaviour over bulk throughput.

## How the Code Fits Together

[main.py](main.py) coordinates two phases:

1. [prepare_migration()](p1_preparation/prepare.py) returns the customer source for preview, or builds and tests three snapshots and [checks their destination mappings](p1_preparation/check_models.py) before returning their names for loading.
2. [migrate_records()](p2_migration/migrate.py) coordinates customers, [products](p2_migration/products.py), then [licences](p2_migration/licences.py). Each stage reads batches, validates and maps the rows, then saves and verifies Salesforce records. Load reports retain all three snapshot names; preview still reads only customers.

The numbered folders make the flow easy to follow: `p0_data/` holds the sample CSV, `p1_preparation/` owns dbt and model checks, and `p2_migration/` contains the Databricks reader, Salesforce requests, mapping and validation. `shared/console.py` prints plain-text tables without extra dependencies. `main.py` controls execution. Each phase explains its expected failures; unexpected programming errors retain their traceback for debugging.

[stage_customers.sql](p1_preparation/dbt/models/stage_customers.sql) trims surrounding spaces, capitalises country codes and turns empty values into nulls. IDs stay unchanged. Seven dbt data checks cover missing values and duplicate or blank IDs; Python also checks country and postcode formats.

The dbt model is still called `stage_customers`. Its database table is named `workspace.migration.customers_snapshot_<unique_id>`; dbt calls this alternate name an **alias**. Python supplies a fresh name for every load. The full name appears in the terminal and in the migration report's first record. Both phases use the existing `.env` settings.

Snapshots remain in Databricks for inspection, including after a failed data check. Do not edit or replace them. Retaining them uses storage; automatic cleanup is not implemented. A new `--load` creates fresh snapshots; `--resume` continues the original run from its saved snapshots. These are stored copies for one load, not dbt's separate change-history feature called `snapshot`. Original source tables and any old `stage_customers` view are left untouched.

## Check an Interrupted Load

Use the report path printed by the load, replacing `YOUR_RUN_ID` below:

```bash
python main.py --reconcile reports/YOUR_RUN_ID.jsonl
```

This finds an Account, Product or Licence load attempt without recorded verification. It reads the corresponding saved snapshot and compares all mapped fields with Salesforce. It does not run dbt, save records, change the report or resume loading. `--reconcile` cannot be combined with `--load`.

Matching fields mean the record matches **now**, not that a particular request succeeded or its automation ran only once. Differences, an unavailable record or a failed check produce a nonzero exit. With no recorded uncertain attempt, the command exits successfully without contacting either service; this does not establish that the migration finished or recheck previously verified records.

New reports record `DATABRICKS_SERVER_HOSTNAME` and `SALESFORCE_MY_DOMAIN_URL`, without tokens or client credentials. Before checking an uncertain record, reconciliation requires those addresses to match the current settings exactly. This checks configured addresses, not permanent workspace or organisation IDs. The snapshot must still exist unchanged. Older reports missing the addresses or matching attempt entries, and malformed reports, require manual inspection. A [Salesforce lookup returning HTTP 404](https://developer.salesforce.com/docs/platform/api-rest/guide/resources-sobject-upsert-get.html) stays unresolved rather than being treated as permission to create a record.

## Resume an Interrupted Load

This command **can save Accounts, Products and Licences**. Use the original report, with the same connection addresses, unchanged snapshot and field mapping:

```bash
python main.py --resume reports/YOUR_RUN_ID.jsonl
```

Resume skips previously verified records, checks any uncertain save by readback, then continues the stages included in that run. A differing or unavailable uncertain record stops the resume before further saves. Rejected records are checked again and keep the outcome incomplete if still invalid. Older reports retain their original scope: naming a product or licence snapshot does not mean that stage was included. A completed run does nothing. `--load`, `--reconcile` and `--resume` are separate modes.

Progress is appended to the same report. `resume_started` marks each attempt; `account_reconciled`, `product_reconciled` or `licence_reconciled` records matching fields without claiming another create or update. If resume is interrupted, use the same command and report again. Keep the report intact; truncated or contradictory history requires manual inspection.

The final verified total includes previous verifications, reconciled records and successful saves. Created, updated and rejected counts describe the latest attempt. Previously verified records are not fully rechecked in Salesforce. Licence loading does look up its parents again to resolve their current Salesforce IDs; it does not recheck all their descriptive fields.

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
