# Design Decisions

1. **Indexes.** A unique index on `employees.emp_code` enforces unique business identifiers. A unique compound index on `(emp_code, date)` ensures at most one attendance record per employee per day. Additional indexes should support actual filtering and sorting patterns. Redundant indexes should be avoided unless query-plan analysis demonstrates a measurable benefit.

2. **Punch-in concurrency.** Concurrent requests may both pass an initial existence check. The unique compound index remains the final safeguard against duplicate attendance records. Duplicate-key errors are handled and translated into HTTP 409 Conflict responses.

3. **Leaderboard ties.** The leaderboard uses competition ranking: employees with equal totals share a rank, and the next rank is skipped. If the contract requires including all employees tied at the cutoff rank, the limit must be applied by rank rather than by array position.

4. **Department headcount.** Department summaries should start from the eligible employee set and join attendance data so employees without attendance logs are not excluded from headcount calculations.

5. **Scaling and index selection.** At substantially higher data volumes, I would benchmark representative queries, inspect MongoDB execution plans, evaluate index selectivity, and measure aggregation latency before introducing or changing indexes.
