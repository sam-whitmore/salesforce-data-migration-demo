# What the tests protect

Each test names a behaviour and begins with a comment explaining why it matters. Arrange sets up the example, Act runs the operation, and Assert checks the result. Tests use temporary files and simulated external services; they do not need credentials.

Run the Python suite from the repository root:

```bash
PYTHON_DOTENV_DISABLED=1 python -m unittest discover -s tests -v
```

## Responsibilities

| File | Question it answers |
| --- | --- |
| [test_validation.py](test_validation.py) | Which customer values are acceptable, and can we report problems without silently changing the input? |
| [test_mapping.py](test_mapping.py) | Do the intended fields reach the Account payload, with stable customer IDs and an unchanged source record? |
| [test_databricks.py](test_databricks.py) | Are table names passed correctly, count results kept in order, and batches fetched only as needed? |
| [test_preparation.py](test_preparation.py) | Does Python build all three snapshots, check their destination models and skip those steps during preview? |
| [test_dbt_output.py](test_dbt_output.py) | Does compact progress respond to real dbt event messages, and does the summary distinguish warnings, failures and skipped checks? |
| [test_retries.py](test_retries.py) | Which requests may repeat, how long should they wait, and when must retries stop? |
| [test_salesforce.py](test_salesforce.py) | Are requests authenticated and addressed correctly, and is each successful save followed by verification without unsafe replay? |
| [test_models.py](test_models.py) | Do preparation checks compare actual snapshot columns and Salesforce metadata, explain mismatches and avoid writing records? |
| [test_products.py](test_products.py) | Do products retain their active/inactive state, save and verify correctly, and recover safely without expanding older customer-only runs? |
| [test_licence_data.py](test_licence_data.py) | Do licence dates, seats, IDs and mappings follow the agreed rules, and are snapshot reads bounded? |
| [test_licences.py](test_licences.py) | Do all sample records load in parent-first order, retain their relationships and terms, update without duplicates and recover safely after interruption? |
| [test_main.py](test_main.py) | Do the phases work together, preserve preview behaviour, and report complete, incomplete and failed loads accurately? |
| [test_reporting.py](test_reporting.py) | Is the pending account still recorded when a process exits without closing its report or writing a final summary? |
| [test_reconcile.py](test_reconcile.py) | Does reconciliation check the destinations, select the uncertain attempt, compare its saved snapshot, and leave accounts and reports unchanged? |
| [test_resume.py](test_resume.py) | Does resume skip recorded successes, resolve uncertainty without another save, and preserve recoverable progress through another interruption? |

## Similar examples are not always duplicate tests

The retry tests exercise the policy directly. The Salesforce tests check that the actual request functions apply it correctly. Removing `read_only=True` from the Account read could leave the policy tests passing while breaking the Salesforce tests.

The customer workflow tests answer another question: after a request fails, does processing stop and does the saved report describe the partial progress? They replace the Salesforce operation, so they do not retest its HTTP handling.

Product and licence workflow tests also exercise the real Salesforce request functions with simulated HTTP responses. They check object routing, durable write intent, separate totals, readback failures and interrupted-load recovery together. Licence tests run the committed CSV examples through simulated services, checking every parent link and term. They cover missing parents, rejected records and preservation of older run scopes.

The reporting test deliberately stops a separate local process after recording an attempt. It checks that the report survives an abrupt process exit; it does not simulate a power failure or guarantee disk hardware behaviour.

Resume tests simulate a save followed by a lost response, then resume again and check that the save is not repeated. They also exercise the local file lock, blocked recovery, storage failure, completed runs and rejected customers. These checks use simulated services and do not establish live recovery performance or exactly-once Salesforce automation.

Several input variations use `subTest`, such as short, long and non-numeric postcodes. Each case still runs and failures identify its input. Grouping them keeps one rule together; it does not remove scenarios. Test-method count is therefore not a measure of coverage.

An empty source, all customers rejected, and a mixture of accepted and rejected customers remain separate tests because their outcomes differ.

## What these checks do not establish

Python tests do not execute SQL or call Salesforce. They cannot establish real permissions, database isolation, network behaviour or large-scale performance. The Databricks count test checks parameter binding and returned values, not the SQL engine's calculation.

The three [customer SQL unit tests](../p1_preparation/dbt/models/stage_customers_unit_tests.yml) execute transformations against controlled examples on Databricks. Seven data checks inspect the customer snapshot. The [product](../p1_preparation/dbt/models/stage_products_unit_tests.yml) and [licence](../p1_preparation/dbt/models/stage_licences_unit_tests.yml) definitions add three unit tests and 17 data checks, including parent relationships and seat/date rules. `--load` selects all of them before any Salesforce access. All six unit tests and 24 data checks passed in a live Databricks run. GitHub's dbt parse step checks configuration only.

When adding a test, identify the incorrect behaviour it should catch. Keep a separate test when the rule or observable outcome differs. Use another subtest case when only the example input changes.
