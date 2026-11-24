# Salesforce Data Migration and Integration

## Purpose and Current Status

I created this repository in response to Gallagher's advertised [Data Engineer](https://careers.gallagher.com/jobs/8676662-data-engineer) opportunity. I am building this project to show how I would approach moving customer and licence data from other business systems into Salesforce, then checking that it arrived correctly.

So far, I have uploaded [data/seed/customers_v1.csv](data/seed/customers_v1.csv) to Databricks and queried it from Python. The program counts the rows and the different customer IDs in the table. Both counts returned 20, matching the CSV file.

This confirms that the connection works and the counts match. The program does not yet check each customer's details, change the data or send it to Salesforce. I have created a Salesforce Developer Edition account, but have not connected the program to it yet.

## Run It on Your Computer

You will need Python 3.13 and your own Databricks workspace with a SQL warehouse, which runs SQL queries. These steps are for macOS or Linux. Open a terminal in this repository's folder before running the commands. The program has been tested with Python 3.13.18.

### 1. Upload the customer CSV

In Databricks, create a table by uploading [customers_v1.csv](data/seed/customers_v1.csv):

- Use the first row as column names, a comma as the separator and UTF-8 encoding.
- Import every column as a string (text). This keeps values such as the postcode `0110` unchanged.
- Note the table's full name, including its catalog and schema. Mine is `workspace.default.customers_v1`.

The CSV contains 20 fictional organisations, not Gallagher customer data. Names, street addresses and contact details are invented; websites and email addresses ending in `.example` are placeholders. Each customer has a UUIDv7 ID. Keep these IDs unchanged when uploading the file again so the same customer can be recognised across runs.

### 2. Install the Python packages

Create a virtual environment to keep this project's Python packages separate from other projects, then install the packages listed in `requirements.txt`:

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

### 3. Configure the connection

Copy the example settings into a local `.env` file. This command keeps any existing `.env` file:

```bash
cp -n .env.example .env
```

Fill in these settings in `.env`. Find the hostname and HTTP path under **SQL Warehouses → your warehouse → Connection Details** in Databricks.

| Setting | Value |
| --- | --- |
| `DATABRICKS_SERVER_HOSTNAME` | Server Hostname, without `https://` |
| `DATABRICKS_HTTP_PATH` | HTTP Path for the warehouse |
| `DATABRICKS_TOKEN` | A personal access token with permission to use the warehouse and read the source table |
| `DATABRICKS_CUSTOMERS_TABLE` | Your table's full name; the example uses `workspace.default.customers_v1` |

The workspace ID and token ID entries can stay blank; the program does not use them. Keep the actual token in `.env`. Git ignores `.env` and `.venv`, while `.env.example` shows other developers which settings are needed. If a setting is already defined in the terminal environment, that value takes priority over the value in `.env`.

### 4. Run the query

```bash
python main.py
```

With the CSV uploaded unchanged, the expected output is:

```text
Total customers: 20
Unique customer IDs: 20
```

You may also see a warning that PyArrow is not installed. It is optional and is not needed for this query. The program prints the counts; it does not yet check them automatically.

## Tools and Progress

The advertised role helped guide these choices. This table separates what works today from what is planned or still being considered.

| Tool | What it is for | Progress |
| --- | --- | --- |
| Python and SQL | Read, check and prepare customer data | The count query works; further processing is planned |
| Salesforce Developer Edition | Receive the customer and licence records | Account created; program not yet connected |
| Databricks SQL | Store the uploaded data and run SQL queries | CSV uploaded and queried successfully from Python |
| PySpark | Process data in Databricks | Planned |
| dbt Core | Organise SQL transformations, data checks and documentation | Planned |
| Azure Data Factory | Schedule and coordinate the pipeline's steps | Under consideration |
| SAP Business Partner API | Read customer data from SAP | Test environment access still to investigate |
| GitHub Actions | Run tests automatically, with deployment considered later | Planned |
| Genesys Cloud and SharePoint | Possible additional sources or destinations | Outside the first version |
