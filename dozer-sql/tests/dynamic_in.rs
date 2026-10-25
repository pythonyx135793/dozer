#[path = "support/dynamic_in.rs"]
mod support;
use dozer_types::types::{Field, FieldType, Operation, Record};
use support::*;

#[test]
fn live_outer_and_inner_changes_reach_the_real_sink() {
    let mut steps = vec![
        step("outer arrives first", 0, insert(pair(1, 10)), vec![]),
        step(
            "first membership",
            1,
            insert(pair(1, 100)),
            vec![pair(1, 10)],
        ),
        step(
            "duplicate inner",
            1,
            insert(pair(1, 200)),
            vec![pair(1, 10)],
        ),
        step(
            "one duplicate removed",
            1,
            delete(pair(1, 100)),
            vec![pair(1, 10)],
        ),
        step("last duplicate removed", 1, delete(pair(1, 200)), vec![]),
        step(
            "membership restored",
            1,
            insert(pair(1, 300)),
            vec![pair(1, 10)],
        ),
        step(
            "matching outer payload update",
            0,
            update(pair(1, 10), pair(1, 11)),
            vec![pair(1, 11)],
        ),
        step(
            "outer leaves membership",
            0,
            update(pair(1, 11), pair(2, 20)),
            vec![],
        ),
        step(
            "outer remains excluded",
            0,
            update(pair(2, 20), pair(3, 30)),
            vec![],
        ),
        step(
            "outer enters membership",
            0,
            update(pair(3, 30), pair(1, 12)),
            vec![pair(1, 12)],
        ),
        step(
            "inner payload-only update",
            1,
            update(pair(1, 300), pair(1, 301)),
            vec![pair(1, 12)],
        ),
        step(
            "inner key update retracts",
            1,
            update(pair(1, 301), pair(2, 301)),
            vec![],
        ),
        step(
            "matching outer delete after exclusion",
            0,
            delete(pair(1, 12)),
            vec![],
        ),
        step(
            "inner arrived before outer",
            0,
            insert(pair(2, 22)),
            vec![pair(2, 22)],
        ),
        step("outer delete", 0, delete(pair(2, 22)), vec![]),
    ];
    steps[2].emitted = Some(vec![]);
    steps[3].emitted = Some(vec![]);
    steps[6].emitted = Some(vec![update(pair(1, 10), pair(1, 11))]);
    steps[10].emitted = Some(vec![]);
    let schema = run(
        "SELECT * FROM outer_rows WHERE key IN (SELECT key FROM members)",
        tables(),
        steps,
        true,
        false,
    )
    .unwrap();
    assert_eq!(schema.fields.len(), 2);
    assert_eq!(
        schema
            .fields
            .iter()
            .map(|field| field.name.as_str())
            .collect::<Vec<_>>(),
        ["key", "payload"]
    );
}

#[test]
fn duplicates_and_batches_keep_outer_multiplicity() {
    run(
        "SELECT key FROM outer_rows WHERE key IN (SELECT key FROM members)",
        tables(),
        vec![
            step(
                "duplicate outer batch",
                0,
                Operation::BatchInsert {
                    new: vec![pair(1, 10), pair(1, 10), pair(2, 20)],
                },
                vec![],
            ),
            step(
                "inner duplicate batch",
                1,
                Operation::BatchInsert {
                    new: vec![pair(1, 1), pair(1, 2)],
                },
                vec![row(1), row(1)],
            ),
            step("delete one outer", 0, delete(pair(1, 10)), vec![row(1)]),
            step("delete one inner", 1, delete(pair(1, 1)), vec![row(1)]),
            step("delete last inner", 1, delete(pair(1, 2)), vec![]),
            step(
                "remaining outer can return",
                1,
                insert(pair(1, 3)),
                vec![row(1)],
            ),
        ],
        true,
        false,
    )
    .unwrap();
}

#[test]
fn not_in_empty_null_and_live_null_removal() {
    let null_pair = Record::new(vec![Field::Null, Field::Int(0)]);
    run(
        "SELECT key FROM outer_rows WHERE key NOT IN (SELECT key FROM members)",
        tables(),
        vec![
            step(
                "NULL outer with empty RHS",
                0,
                insert(null_pair.clone()),
                vec![null()],
            ),
            step(
                "non-NULL outer with empty RHS",
                0,
                insert(pair(2, 0)),
                vec![null(), row(2)],
            ),
            step(
                "nonempty RHS makes NULL outer unknown",
                1,
                insert(pair(1, 0)),
                vec![row(2)],
            ),
            step(
                "NULL RHS makes unmatched outer unknown",
                1,
                insert(null_pair.clone()),
                vec![],
            ),
            step(
                "last NULL removed",
                1,
                delete(null_pair.clone()),
                vec![row(2)],
            ),
            step(
                "last RHS removed",
                1,
                delete(pair(1, 0)),
                vec![null(), row(2)],
            ),
            step("NULL-only RHS", 1, insert(null_pair.clone()), vec![]),
            step(
                "NULL updated to matching value",
                1,
                update(null_pair, pair(2, 0)),
                vec![],
            ),
            step(
                "empty RHS restored",
                1,
                delete(pair(2, 0)),
                vec![null(), row(2)],
            ),
        ],
        true,
        false,
    )
    .unwrap();
}

#[test]
fn compound_multiple_and_nested_expressions() {
    run("SELECT key FROM outer_rows WHERE NOT (key IN (SELECT key FROM members) AND payload = 0) OR key IN (SELECT key FROM others)", tables(), vec![
        step("outer excluded by first input", 1, insert(pair(1, 0)), vec![]),
        step("outer", 0, insert(pair(1, 0)), vec![]),
        step("second membership overrides", 2, insert(pair(1, 0)), vec![row(1)]),
        step("second membership removed", 2, delete(pair(1, 0)), vec![]),
        step("first becomes unknown", 1, update(pair(1, 0), Record::new(vec![Field::Null, Field::Int(0)])), vec![]),
        step("false AND unknown negates to true", 0, update(pair(1, 0), pair(1, 1)), vec![row(1)]),
    ], true, false).unwrap();
    run("SELECT key FROM outer_rows WHERE CASE WHEN (key IN (SELECT key FROM members)) IS TRUE THEN 7 ELSE 0 END = 7", tables(), vec![
        step("outer", 0, insert(pair(1, 0)), vec![]),
        step("case becomes true", 1, insert(pair(1, 0)), vec![row(1)]),
        step("case becomes false", 1, delete(pair(1, 0)), vec![]),
    ], true, false).unwrap();
}

#[test]
fn scalar_projection_filter_alias_and_derived_table() {
    run("SELECT o.key FROM outer_rows o WHERE o.key IN (SELECT d.shifted FROM (SELECT m.key + 1 AS shifted FROM members m WHERE m.payload > 0) d)", tables(), vec![
        step("outer", 0, insert(pair(2, 0)), vec![]),
        step("filtered inner", 1, insert(pair(1, 0)), vec![]),
        step("inner enters filter", 1, update(pair(1, 0), pair(1, 1)), vec![row(2)]),
        step("projection changes", 1, update(pair(1, 1), pair(3, 1)), vec![]),
        step("outer matches projected value", 0, update(pair(2, 0), pair(4, 0)), vec![row(4)]),
        step("inner leaves filter", 1, update(pair(3, 1), pair(3, 0)), vec![]),
    ], true, false).unwrap();
}

#[test]
fn inner_join_changes_drive_membership() {
    run("SELECT key FROM outer_rows WHERE key IN (SELECT m.key FROM members m JOIN others x ON m.key = x.key)", tables(), vec![
        step("outer", 0, insert(pair(1, 0)), vec![]),
        step("one join side", 1, insert(pair(1, 0)), vec![]),
        step("join becomes populated", 2, insert(pair(1, 5)), vec![row(1)]),
        step("duplicate join match", 2, insert(pair(1, 6)), vec![row(1)]),
        step("one match deleted", 2, delete(pair(1, 5)), vec![row(1)]),
        step("last match leaves", 2, update(pair(1, 6), pair(2, 6)), vec![]),
    ], true, false).unwrap();
}

#[test]
fn inner_aggregation_and_having_updates_are_live() {
    run("SELECT key FROM outer_rows WHERE key IN (SELECT COUNT(key) FROM members GROUP BY payload HAVING COUNT(key) > 1)", tables(), vec![
        step("outer batch", 0, Operation::BatchInsert { new: vec![pair(1, 0), pair(2, 0), pair(3, 0)] }, vec![]),
        step("count one excluded", 1, insert(pair(10, 7)), vec![]),
        step("count two enters HAVING", 1, insert(pair(11, 7)), vec![row(2)]),
        step("count three changes membership", 1, insert(pair(12, 7)), vec![row(3)]),
        step("count two restored", 1, delete(pair(11, 7)), vec![row(2)]),
        step("count one leaves HAVING", 1, delete(pair(12, 7)), vec![]),
    ], true, false).unwrap();
}

#[test]
fn inherited_cte_and_local_shadowing_remain_scoped() {
    run("WITH eligible AS (SELECT key FROM members WHERE payload > 0) SELECT key FROM outer_rows WHERE key IN (SELECT key FROM eligible) AND key NOT IN (WITH eligible AS (SELECT key FROM others) SELECT key FROM eligible)", tables(), vec![
        step("outer", 0, insert(pair(1, 0)), vec![]),
        step("inherited CTE", 1, insert(pair(1, 1)), vec![row(1)]),
        step("local CTE shadows inherited", 2, insert(pair(1, 0)), vec![]),
        step("local CTE retracts", 2, delete(pair(1, 0)), vec![row(1)]),
        step("inherited CTE retracts", 1, delete(pair(1, 1)), vec![]),
    ], true, false).unwrap();
}

#[test]
fn nested_membership_and_parenthesized_queries() {
    run("SELECT key FROM outer_rows WHERE key IN ((SELECT key FROM members WHERE key IN (SELECT key FROM others WHERE key NOT IN (SELECT key FROM more))))", tables(), vec![
        step("outer", 0, insert(pair(1, 0)), vec![]),
        step("first level", 1, insert(pair(1, 0)), vec![]),
        step("second level", 2, insert(pair(1, 0)), vec![row(1)]),
        step("third level excludes", 3, insert(pair(1, 0)), vec![]),
        step("third level retracts", 3, delete(pair(1, 0)), vec![row(1)]),
        step("second level retracts", 2, delete(pair(1, 0)), vec![]),
    ], true, false).unwrap();
}

#[test]
fn union_all_and_distinct_membership_retract_last_copy() {
    for quantifier in ["ALL", "DISTINCT"] {
        run(&format!("SELECT key FROM outer_rows WHERE key IN (SELECT key FROM members UNION {quantifier} SELECT key FROM others)"), tables(), vec![
            step("outer", 0, insert(pair(1, 0)), vec![]),
            step("left union", 1, insert(pair(1, 1)), vec![row(1)]),
            step("right union duplicate", 2, insert(pair(1, 2)), vec![row(1)]),
            step("left union retracts", 1, delete(pair(1, 1)), vec![row(1)]),
            step("right union retracts", 2, delete(pair(1, 2)), vec![]),
        ], true, false).unwrap();
    }
}

#[test]
fn same_source_can_feed_both_sides_without_id_collisions() {
    run(
        "SELECT key FROM outer_rows WHERE key IN (SELECT key FROM outer_rows WHERE payload > 0)",
        tables(),
        vec![
            step(
                "row does not qualify as member",
                0,
                insert(pair(1, 0)),
                vec![],
            ),
            step(
                "same source establishes membership",
                0,
                insert(pair(1, 1)),
                vec![row(1), row(1)],
            ),
            step("qualifying row removed", 0, delete(pair(1, 1)), vec![]),
        ],
        true,
        false,
    )
    .unwrap();
}

#[test]
fn original_static_in_null_semantics_remain_unchanged() {
    let value = Record::new(vec![Field::Null, Field::Int(0)]);
    run(
        "SELECT key FROM outer_rows WHERE key IN (NULL)",
        tables(),
        vec![step(
            "static NULL equality",
            0,
            insert(value.clone()),
            vec![null()],
        )],
        true,
        false,
    )
    .unwrap();
    run(
        "SELECT key FROM outer_rows WHERE key IN (NULL) OR key IN (SELECT key FROM members)",
        tables(),
        vec![step(
            "static NULL equality inside dynamic filter",
            0,
            insert(value),
            vec![null()],
        )],
        true,
        false,
    )
    .unwrap();
}

#[test]
fn unsupported_subqueries_fail_explicitly_before_ingestion() {
    for (sql, expected) in [
        ("SELECT o.key FROM outer_rows o WHERE o.key IN (SELECT m.key FROM members m WHERE m.key = o.key)", "Correlated or unknown qualifier"),
        ("SELECT o.key FROM outer_rows o WHERE o.key IN (SELECT o.key FROM members m)", "Correlated or unknown qualifier"),
        ("SELECT key FROM outer_rows WHERE key IN (SELECT key, payload FROM members)", "exactly one column"),
        ("SELECT key FROM outer_rows WHERE key IN (SELECT key FROM members ORDER BY key)", "ORDER BY"),
        ("SELECT key FROM outer_rows WHERE key IN (SELECT key FROM members LIMIT 1)", "Limit and Offset"),
        ("SELECT key FROM outer_rows WHERE key IN (SELECT key FROM members FETCH FIRST 1 ROW ONLY)", "FETCH"),
        ("SELECT key FROM outer_rows WHERE key IN (WITH RECURSIVE x AS (SELECT key FROM members) SELECT key FROM x)", "Recursive"),
        ("SELECT key FROM outer_rows WHERE key IN (SELECT key FROM members, others)", "Unsupported SELECT"),
        ("SELECT key FROM outer_rows WHERE key IN (SELECT key FROM LATERAL (SELECT key FROM members) x)", "lateral"),
        ("SELECT key FROM outer_rows WHERE key IN (SELECT key FROM members INTERSECT SELECT key FROM others)", "SELECT and UNION"),
    ] {
        let error = run(sql, tables(), vec![], false, false).unwrap_err().to_string();
        assert!(error.contains(expected), "expected {expected:?}, got {error:?} for {sql}");
    }
    let mut types = tables();
    types[1].1 = schema(&[("key", FieldType::Timestamp)]);
    assert!(run(
        "SELECT key FROM outer_rows WHERE key IN (SELECT key FROM members)",
        types,
        vec![],
        false,
        false
    )
    .unwrap_err()
    .to_string()
    .contains("incompatible"));
    let mut missing = tables();
    missing[1].1 = schema(&[("key", FieldType::Int)]);
    assert!(run(
        "SELECT key FROM outer_rows WHERE key IN (SELECT key FROM members WHERE payload > 0)",
        missing,
        vec![],
        false,
        false
    )
    .is_err());
    assert!(run(
        "SELECT key FROM outer_rows WHERE (key, payload) IN (SELECT key FROM members)",
        tables(),
        vec![],
        false,
        false
    )
    .is_err());
    let mut qualified = tables();
    qualified[1].1 = schema(&[("key", FieldType::Int)]);
    let error = run("SELECT key FROM outer_rows WHERE key IN (SELECT m.payload FROM members m JOIN others x ON m.key = x.key)", qualified, vec![], false, false).unwrap_err().to_string();
    assert!(error.contains("Qualified column"), "{error}");
    let error = run(
        "SELECT key FROM outer_rows WHERE key IN (SELECT key FROM members WHERE NOW() IS NOT NULL)",
        tables(),
        vec![],
        false,
        false,
    )
    .unwrap_err()
    .to_string();
    assert!(error.contains("Volatile"), "{error}");
    let error = run("WITH c AS (SELECT missing.key FROM members) SELECT key FROM outer_rows WHERE key IN (SELECT key FROM c)", tables(), vec![], false, false).unwrap_err().to_string();
    assert!(error.contains("Qualified column"), "{error}");
}

#[test]
fn numeric_schema_compatibility_and_parenthesized_union() {
    let mut types = tables();
    types[1].1 = schema(&[("key", FieldType::UInt), ("payload", FieldType::Int)]);
    run(
        "SELECT key FROM outer_rows WHERE key IN (SELECT key FROM members)",
        types,
        vec![
            step("signed outer", 0, insert(pair(1, 0)), vec![]),
            step(
                "unsigned matching value",
                1,
                insert(Record::new(vec![Field::UInt(1), Field::Int(0)])),
                vec![row(1)],
            ),
        ],
        true,
        false,
    )
    .unwrap();
    run("SELECT key FROM outer_rows WHERE key IN ((SELECT key FROM members) UNION ALL (SELECT key FROM others))", tables(), vec![
        step("outer", 0, insert(pair(1, 0)), vec![]),
        step("parenthesized union branch", 1, insert(pair(1, 0)), vec![row(1)]),
    ], true, false).unwrap();
}

#[test]
fn real_dag_matches_independent_multiset_model_after_each_event() {
    for negated in [false, true] {
        let mut outer = Vec::<Record>::new();
        let mut inner = Vec::<Record>::new();
        let mut steps = vec![];
        let mut seed = 0x6001659_u64;
        for _ in 0..80 {
            seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1);
            let port = ((seed >> 32) & 1) as u16;
            let rows = if port == 0 { &mut outer } else { &mut inner };
            let key = match (seed >> 40) % 5 {
                0 => Field::Null,
                value => Field::Int(value as i64),
            };
            let value = Record::new(vec![key, Field::Int(0)]);
            let action = (seed >> 48) % 4;
            let operation = if rows.is_empty() || action == 0 {
                rows.push(value.clone());
                insert(value)
            } else if action == 1 {
                let old = rows.remove((seed as usize) % rows.len());
                delete(old)
            } else if action == 2 {
                let index = (seed as usize) % rows.len();
                let old = std::mem::replace(&mut rows[index], value.clone());
                update(old, value)
            } else {
                rows.extend([value.clone(), value.clone()]);
                Operation::BatchInsert {
                    new: vec![value.clone(), value],
                }
            };
            let expected = outer
                .iter()
                .filter(|value| {
                    let key = &value.values[0];
                    let truth = if inner.is_empty() {
                        Some(false)
                    } else if key == &Field::Null {
                        None
                    } else if inner.iter().any(|row| &row.values[0] == key) {
                        Some(true)
                    } else if inner.iter().any(|row| row.values[0] == Field::Null) {
                        None
                    } else {
                        Some(false)
                    };
                    truth.map(|truth| truth != negated) == Some(true)
                })
                .map(|row| Record::new(vec![row.values[0].clone()]))
                .collect();
            steps.push(step("model transition", port, operation, expected));
        }
        run(
            &format!(
                "SELECT key FROM outer_rows WHERE key {}IN (SELECT key FROM members)",
                if negated { "NOT " } else { "" }
            ),
            tables(),
            steps,
            true,
            false,
        )
        .unwrap();
    }
}
