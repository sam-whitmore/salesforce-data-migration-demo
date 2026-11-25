select
    product_id,
    nullif(trim(product_code), '') as product_code,
    nullif(trim(product_name), '') as product_name,
    is_active
from {{ source('crm', 'products_v1') }}
