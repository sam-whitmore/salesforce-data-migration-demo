# Salesforce Data Migration and Integration

## Purpose and Progress

I created this project in response to Gallagher's advertised [Data Engineer](https://careers.gallagher.com/jobs/8676662-data-engineer) opportunity. The goal is to move customer and licence data into Salesforce and check that it arrived correctly.

So far, Python reads 20 fictional customers from Databricks, six at a time, checks their values, and prepares valid records using Salesforce's field names. It reports rejected records and their reasons. Prepared accounts are held only for the current batch; nothing is saved or sent to Salesforce yet. Performance with large datasets has not yet been tested.

The checks require non-empty text for customer IDs, names, cities, postcodes and country codes. Country codes must have two uppercase letters; NZ postcodes must have [four digits, including leading zeros](https://www.nzpost.co.nz/personal/sending-in-nz/postcodes). Values are checked without being changed. These are basic format checks, not address verification or duplicate detection.

## Preparing Salesforce Accounts

| Customer field | Salesforce Account field |
| --- | --- |
| `customer_id` | `Source_Customer_ID__c` (custom) |
| `legal_name` | `Name` |
| `billing_city` | `BillingCity` |
| `billing_postal_code` | `BillingPostalCode` |
| `billing_country_code` | `BillingCountryCode` |

Before connecting Salesforce, create `Source_Customer_ID__c` on Account as Text (36), marked **External ID** and **Unique**. It will hold the existing customer UUID so later runs can [match and update the same account](https://developer.salesforce.com/docs/platform/api-rest/guide/dome-upsert.html). `BillingCountryCode` requires [State and Country/Territory Picklists](https://help.salesforce.com/s/articleView?id=000220089&language=en_US&type=1), with the source countries enabled. These settings and Salesforce's acceptance of the prepared records have not yet been verified.

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

Fill in these four settings in `.env`:

| Setting | What to enter |
| --- | --- |
| `DATABRICKS_SERVER_HOSTNAME` | Warehouse hostname, without `https://` |
| `DATABRICKS_HTTP_PATH` | Warehouse HTTP path |
| `DATABRICKS_TOKEN` | Personal access token with permission to query the table |
| `DATABRICKS_CUSTOMERS_TABLE` | Full table name, such as `workspace.default.customers_v1` |

Find the hostname and HTTP path under **SQL Warehouses → your warehouse → Connection Details**. The workspace ID and token ID entries are unused and can stay blank. Git ignores `.env` and `.venv`; keep credentials in `.env`.

### 3. Run

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

## Tests

```bash
python -m unittest discover -s tests -v
```

The tests cover batching, validation and field mapping using sample records, so no credentials are needed. They check invalid values, preserve leading zeros and customer IDs, and confirm that the original data stays unchanged. They do not connect to Databricks or Salesforce.
