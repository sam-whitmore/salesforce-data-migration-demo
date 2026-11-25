-- Preserve historical and future terms; validation reports problems without dropping rows.
select
    licence_id,
    customer_id,
    product_id,
    seats,
    starts_on,
    ends_before,
    cancelled_on
from {{ source('crm', 'licences_v1') }}
