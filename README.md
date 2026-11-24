# Salesforce Data Migration and Integration

## Purpose and Progress

I created this project in response to Gallagher's advertised [Data Engineer](https://careers.gallagher.com/jobs/8676662-data-engineer) opportunity. The goal is to move customer and licence data into Salesforce and check that it arrived correctly.

Python reads fictional customers from Databricks in batches, validates them, and prepares Salesforce accounts. By default it previews the original source without writing to either system. With `--load`, dbt first prepares and tests a customer view. Python then reads that view, saves each valid account and reads it back to check all five mapped fields. All 20 sample customers were loaded and verified before dbt was added; the integrated dbt workflow has not yet been run against the live systems. Large-scale performance has not been tested.

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
| `DATABRICKS_TOKEN` | Personal access token; loading also needs permission to create the dbt schema and view |
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
Total customers retrieved: 20
Valid customers: 20
Rejected customers: 0
Accounts prepared for Salesforce: 20
```

### 4. Load and verify the customers

This command **builds and tests a Databricks view, then creates or updates Salesforce Accounts**. If dbt fails, no Salesforce requests are made:

```bash
python main.py --load
```

Each batch is prepared, saved and checked before the next batch is fetched. Salesforce requests currently run one account at a time, using one login for the run. The final report counts created, updated and verified accounts.

Final output from the verified run on 25 November 2026:

```text
Total customers retrieved: 20
Valid customers: 20
Rejected customers: 0
Accounts prepared for Salesforce: 20
Accounts created: 19
Accounts updated: 1
Accounts verified: 20
```

One account already existed from the single-customer check. All five mapped fields matched for every account. A subsequent run updated all 20 accounts without creating new ones.

During migration, customers rejected by the Python checks are reported and skipped. A failed dbt data check stops the entire run before migration. A save or verification failure stops the run; earlier writes remain in Salesforce. Rerunning with the same source IDs updates the same accounts. Recovery currently means rerunning, rather than resuming from a saved checkpoint.

The loader uses Salesforce API v68.0 and updates only the mapped fields. Keep the source table unchanged during a run; the view is not a snapshot, and testing and extraction are separate queries.

## How the Code Fits Together

[main.py](main.py) coordinates two phases:

1. [prepare_customers()](p1_preparation/prepare.py) selects the original source for preview. With `--load`, it builds and tests the dbt view and returns its name.
2. [migrate_customers()](p2_migration/migrate.py) reads that source in batches, validates and maps the customers, and optionally saves and verifies their Salesforce accounts.

The numbered folders make the flow easy to follow: `p0_data/` holds the sample CSV, `p1_preparation/` owns dbt, and `p2_migration/` contains the Databricks reader, Salesforce requests, mapping and validation. `main.py` controls execution. Each phase explains its expected failures; unexpected programming errors retain their traceback for debugging.

[stage_customers.sql](p1_preparation/dbt/models/stage_customers.sql) trims surrounding spaces, capitalises country codes and turns empty values into nulls. IDs stay unchanged. Seven dbt data checks cover missing values and duplicate or blank IDs; Python also checks country and postcode formats.

The view is currently `workspace.migration.stage_customers`, configured through [profiles.yml](p1_preparation/dbt/profiles.yml) and the model name. The original table is unchanged. A failed data check stops migration but does not remove the view. Both phases use the existing `.env` settings; no separate helper commands or credentials are needed.

## Tests

```bash
python -m unittest discover -s tests -v
```

The tests cover extraction, validation, mapping, Salesforce requests and the batch workflow, including failures. They use sample records and simulated responses, without credentials or live connections.

[GitHub Actions](.github/workflows/tests.yml) runs the same tests and checks the dbt project configuration on each push and pull request, using Python 3.13 on Linux. Results appear in the repository's **Actions** tab. These checks need no credentials; dbt's SQL and data tests run against Databricks as the first phase of `python main.py --load`.
