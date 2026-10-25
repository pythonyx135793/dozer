# Streaming membership

`WHERE value IN (SELECT ...)` and `NOT IN` connect the outer stream and each
uncorrelated subquery to a stateful selection processor. Both sides remain live:
inserts, deletes, updates and batches can change the selected outer multiset.
Inner duplicates are reference-counted; each retained outer occurrence is emitted
or retracted only when its predicate changes selectedness. Inner updates and
batches are evaluated as a complete change, without intermediate membership
retractions. Outer operations retain their operation IDs, fields and lifetimes.

Dynamic predicates use SQL three-valued `AND`, `OR` and `NOT`. An empty RHS makes
`IN` false, including for a NULL operand; otherwise a matching non-NULL value is
true, and an unmatched comparison involving NULL is unknown. `NOT IN` negates
that truth value. Only true selects a row. Existing static `IN (...)` evaluation,
including its established NULL equality, is unchanged.

The existing planner supplies inner projection, filtering, joins, grouping,
aggregation/HAVING, derived tables, non-recursive CTEs and UNION. Multiple and
nested membership predicates have separate ports and globally unique processor
IDs. CTE definitions inherit lexical scope without leaking inner definitions.
Parenthesized queries preserve their output connection. Inner output must have
exactly one column. Numeric types and string/text pairs use the existing equality
operator; unrelated types and binary/JSON comparisons are rejected explicitly.

Correlated/lateral subqueries, row-valued membership, recursive CTEs and the
planner's unsupported relational clauses are rejected. Qualified inner columns
must belong to the actual local schema, even when another table has a unique
column with that name. Volatile `NOW()` and configured UDF calls are rejected in
dynamic predicates/subqueries because reevaluating retained rows must be
repeatable. This change does not add subquery expressions to SELECT or HAVING.

State is in memory, following the existing processor contract; this does not add
restart persistence. Duplicate count changes avoid reevaluation. When distinct
membership, NULL presence or emptiness changes, the general predicate is
reevaluated for retained distinct outer records. This favors correct compound
predicates over an equality-only outer index; worst-case work is proportional to
retained rows and distinct inner values. Memory is proportional to the retained
outer records and distinct inner values. Ordering between different output rows
is unspecified.

`tests/dynamic_in.rs` executes the actual SQL builder, schemas, DAG, in-memory
source ingestion and collecting sink. An executor commit barrier follows each
event; tests assert the full materialized multiset without sleeps. The unit tests
also assert exact operation transitions, IDs/lifetimes, errors, atomicity and an
independent deterministic multiset model. Run the normal default-feature suites:

```sh
cargo test --locked -p dozer-sql -p dozer-sql-expression -p dozer-core
cargo run --locked -p dozer-sql --example dynamic_in
```

The example prints real input/output operations and verifies every committed
result. It uses only memory sources, with presentation pacing between events;
it needs no connector credentials, database or external service.
