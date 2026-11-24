-- A dbt data test fails if this query returns any rows.
select customer_id
from {{ ref('stage_customers') }}
where trim(customer_id) = ''
