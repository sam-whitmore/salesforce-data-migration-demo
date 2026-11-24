select
    customer_id,
    nullif(trim(legal_name), '') as legal_name,
    nullif(trim(billing_city), '') as billing_city,
    nullif(trim(billing_postal_code), '') as billing_postal_code,
    nullif(upper(trim(billing_country_code)), '') as billing_country_code
from {{ source('crm', 'customers_v1') }}
