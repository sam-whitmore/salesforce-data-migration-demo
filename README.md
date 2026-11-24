# Salesforce Data Migration and Integration

## Purpose and Progress

I created this project in response to Gallagher's advertised [Data Engineer](https://careers.gallagher.com/jobs/8676662-data-engineer) opportunity. The goal is to move customer and licence data into Salesforce and check that it arrived correctly.

Python reads fictional customers from Databricks in batches, validates them, and prepares Salesforce accounts. By default it previews the work. With `--load`, it saves each valid account and reads it back to check all five mapped fields. All 20 sample customers have been loaded and verified in Salesforce. Large-scale performance has not been tested.

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

Upload [customers_v1.csv](data/seed/customers_v1.csv) to Databricks. It contains fictional organisations and contact details, not Gallagher customer data.

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
| `DATABRICKS_TOKEN` | Personal access token with permission to query the table |
| `DATABRICKS_CUSTOMERS_TABLE` | Full table name, such as `workspace.default.customers_v1` |
| `SALESFORCE_MY_DOMAIN_URL` | My Domain URL, including `https://`, without a page path |
| `SALESFORCE_CLIENT_ID` | External Client App consumer key |
| `SALESFORCE_CLIENT_SECRET` | External Client App consumer secret |

Find the hostname and HTTP path under **SQL Warehouses → your warehouse → Connection Details**. The workspace ID and token ID entries are unused and can stay blank. Git ignores `.env` and `.venv`; keep credentials in `.env`.

For Salesforce, configure an [External Client App with Client Credentials Flow](https://help.salesforce.com/s/articleView?id=sf.configure_client_credentials_flow_for_external_client_apps.htm&language=en_US), the `api` scope, and a Run As user with access to Account and the mapped fields.

### 3. Preview the customers

```bash
python main.py
```

With the sample CSV uploaded, expect:

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

An optional PyArrow warning may appear; this query works without it.

### 4. Load and verify the customers

This command **creates or updates Salesforce Accounts** for the valid source records:

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

One account already existed from the single-customer check. All five mapped fields matched for every account.

Invalid customers are reported and skipped. A save or verification failure stops the run; earlier writes remain in Salesforce. Rerunning with the same source IDs updates the same accounts. Recovery currently means rerunning, rather than resuming from a saved checkpoint.

To load just one customer:

```bash
python load_customer.py --customer-id 01a20ada-2c00-7305-8588-34a676a8372c
```

Both commands use Salesforce API v68.0 and update only the mapped fields. Keep the source table unchanged during a run; the initial ID check and extraction are separate queries.

## Tests

```bash
python -m unittest discover -s tests -v
```

The tests cover extraction, validation, mapping, Salesforce requests and the batch workflow, including failures. They use sample records and simulated responses, without credentials or live connections.
