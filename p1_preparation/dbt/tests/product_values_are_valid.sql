-- Missing values and duplicates have separate tests. These checks protect IDs and source types.
select product_id
from {{ ref('stage_products') }}
where trim(product_id) = ''
    or typeof(product_id) <> 'string'
    or typeof(is_active) <> 'boolean'
