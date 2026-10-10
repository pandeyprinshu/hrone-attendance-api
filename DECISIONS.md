# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (uniqueness and sort), `(department, emp_code)` for filtered lists, `joined_on` and `(department, joined_on)` for headcount. `attendance_logs`: unique `(emp_code, date)` (one record per day, race guard, monthly and PATCH lookups), `(date desc, emp_code)` (list sort and month scans), `(emp_code, punch_in desc)` (punch-out lookup). I rejected a separate `status` or `late_minutes` index: they only filter inside a date range, so the extra write cost buys little.

2. **Punch-in race.** Both requests compute the same `(emp_code, date)` and call `insert_one`. The unique index lets exactly one insert win; the other raises `DuplicateKeyError`, which I return as 409. There is no check-then-insert.

3. **Ties.** I rank with `$rank` over everyone first, then keep `rank <= limit`. Both tied employees at the cutoff are returned, so the list can be longer than `limit`. Rows are ordered by late minutes, then `emp_code`.

4. **Headcount.** The summary pipeline starts from `employees` (joined on or before month end) and `$lookup`s their logs. Someone with no logs gets an empty array but is still counted by `$group`.

5. **100x data.** I would stop scanning raw logs for analytics and keep daily or monthly rollups per employee, updated on each punch or correction.
