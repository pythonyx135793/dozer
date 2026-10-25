//! Run with: cargo run -p dozer-sql --example dynamic_in
//! Uses only in-memory sources and the real SQL/DAG executor; no services.
#[path = "../tests/support/dynamic_in.rs"]
#[allow(dead_code)] // The shared integration harness exposes additional test helpers.
mod support;
use dozer_types::{
    errors::internal::BoxedError,
    types::{Field, Record},
};
use support::*;

fn main() -> Result<(), BoxedError> {
    println!("Dozer streaming IN / NOT IN demonstration");
    println!("Real Linux execution; each displayed result follows an executor commit barrier.");
    let mut steps = vec![
        step(
            "Store an outer row before membership exists",
            0,
            insert(pair(1, 10)),
            vec![],
        ),
        step(
            "First inner value inserts the retained outer row",
            1,
            insert(pair(1, 100)),
            vec![pair(1, 10)],
        ),
        step(
            "Duplicate inner value does not duplicate output",
            1,
            insert(pair(1, 200)),
            vec![pair(1, 10)],
        ),
        step(
            "Removing one duplicate keeps the outer row",
            1,
            delete(pair(1, 100)),
            vec![pair(1, 10)],
        ),
        step(
            "Removing the last duplicate retracts the row",
            1,
            delete(pair(1, 200)),
            vec![],
        ),
        step(
            "Membership returns",
            1,
            insert(pair(1, 300)),
            vec![pair(1, 10)],
        ),
        step(
            "A matching outer update is forwarded as UPDATE",
            0,
            update(pair(1, 10), pair(1, 11)),
            vec![pair(1, 11)],
        ),
        step(
            "Changing the inner key retracts old membership",
            1,
            update(pair(1, 300), pair(2, 300)),
            vec![],
        ),
        step(
            "Outer update enters the new membership",
            0,
            update(pair(1, 11), pair(2, 20)),
            vec![pair(2, 20)],
        ),
        step(
            "Deleting the matching outer row retracts it",
            0,
            delete(pair(2, 20)),
            vec![],
        ),
    ];
    steps[2].emitted = Some(vec![]);
    steps[3].emitted = Some(vec![]);
    steps[6].emitted = Some(vec![update(pair(1, 10), pair(1, 11))]);
    run(
        "SELECT * FROM outer_rows WHERE key IN (SELECT key FROM members)",
        tables(),
        steps,
        true,
        true,
    )?;
    let null_pair = Record::new(vec![Field::Null, Field::Int(0)]);
    run(
        "SELECT key FROM outer_rows WHERE key NOT IN (SELECT key FROM members)",
        tables(),
        vec![
            step(
                "NOT IN an empty set selects the outer row",
                0,
                insert(pair(2, 0)),
                vec![row(2)],
            ),
            step(
                "An inner NULL makes unmatched membership unknown",
                1,
                insert(null_pair.clone()),
                vec![],
            ),
            step(
                "Removing NULL restores the selected row",
                1,
                delete(null_pair),
                vec![row(2)],
            ),
            step(
                "A matching value excludes the row",
                1,
                insert(pair(2, 0)),
                vec![],
            ),
        ],
        true,
        true,
    )?;
    println!("\nDEMO COMPLETE: every real committed result matched its asserted multiset.");
    Ok(())
}
