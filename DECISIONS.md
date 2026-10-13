# Design Decisions

1. **Indexes.** A unique `employees.emp_code` index guarantees unique business identifiers. A unique `(emp_code, date)` index guarantees one attendance record per employee/day and resolves simultaneous punch-ins. Additional date and employee/date indexes support list and analytics filters. I would reject redundant indexes unless query plans show a measurable benefit.

2. **Punch-in race.** Both requests may pass an initial read, but only one insert can satisfy the unique compound index. MongoDB rejects the other insert with a duplicate-key error, which the API converts to HTTP 409.

3. **Ties.** The leaderboard uses competition ranking, so tied totals share a rank and the next rank is skipped. The limit is applied to rank, not array position, so all employees tied at the cutoff rank are included.

4. **Headcount.** Department summary starts from eligible employee documents and looks up logs, preserving employees without attendance records.

5. **Scaling.** At 100x volume, I would benchmark real query plans, verify index selectivity, and measure aggregation latency before adding or changing indexes.
