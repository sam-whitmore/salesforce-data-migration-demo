-- Reject incorrect upload types rather than silently rounding seats or converting dates.
select licence_id
from {{ ref('stage_licences') }}
where trim(licence_id) = ''
    or trim(customer_id) = ''
    or trim(product_id) = ''
    or typeof(licence_id) <> 'string'
    or typeof(customer_id) <> 'string'
    or typeof(product_id) <> 'string'
    or typeof(seats) <> 'bigint'
    or typeof(starts_on) <> 'date'
    or typeof(ends_before) <> 'date'
    or typeof(cancelled_on) <> 'date'
    or seats < 1
    or seats > 999999999
    or starts_on >= ends_before
    or cancelled_on < starts_on
    or cancelled_on >= ends_before
