# Starter Code Review

1. `/health` returned success without pinging MongoDB, so it could report readiness while the database was unavailable.
2. Employee pagination used `page * page_size` and counted all employees even when a department filter was active.
3. Punch-in accessed shift data without checking whether the employee existed.
4. Check-then-insert alone is race-prone; a unique `(emp_code, date)` index is required to prevent duplicate attendance records.
5. Attendance listing loaded all matches into Python before sorting and slicing, which does not scale to 100,000 records.
6. Naive local datetimes were used without a consistent UTC/IST boundary policy, risking incorrect attendance dates and epoch values.
7. Punch-out, manual regularization, analytics aggregations, and explain endpoints were missing.

Recommended fixes include idempotent indexes, atomic conditional updates, UTC storage, epoch-millisecond API conversion, database-side pagination, MongoDB aggregation pipelines, and contract-driven validation.
