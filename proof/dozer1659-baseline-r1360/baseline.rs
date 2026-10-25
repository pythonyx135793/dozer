//! Proof-only controls against unchanged application source. Never starts a provider/source.
use std::{collections::HashMap, sync::Arc};
use dozer_core::{app::{App, AppPipeline}, appsource::{AppSourceManager, AppSourceMappings},
    epoch::Epoch, event::EventHub, executor::DagExecutor,
    node::{OutputPortDef, OutputPortType, PortHandle, Sink, SinkFactory, Source, SourceFactory},
    DEFAULT_PORT_HANDLE};
use dozer_sql::builder::statement_to_pipeline;
use dozer_sql_expression::{error::Error, sqlparser::ast::Expr};
use dozer_types::{errors::internal::BoxedError, models::ingestion_types::IngestionMessage,
    node::OpIdentifier, tonic::async_trait,
    types::{FieldDefinition, FieldType, Schema, SourceDefinition, TableOperation}};
use tokio::{runtime::Runtime, sync::mpsc::Sender};

#[derive(Debug)]
struct MemorySource;
impl SourceFactory for MemorySource {
    fn get_output_ports(&self) -> Vec<OutputPortDef> {
        vec![OutputPortDef::new(0, OutputPortType::Stateless), OutputPortDef::new(1, OutputPortType::Stateless)]
    }
    fn get_output_schema(&self, _: &PortHandle) -> Result<Schema, BoxedError> {
        Ok(Schema::default().field(FieldDefinition::new("CustomerID".into(), FieldType::Int, false, SourceDefinition::Dynamic), false).clone())
    }
    fn get_output_port_name(&self, port: &PortHandle) -> String { format!("port_{port}") }
    fn build(&self, _: HashMap<PortHandle, Schema>, _: EventHub, _: Option<Vec<u8>>) -> Result<Box<dyn Source>, BoxedError> {
        Ok(Box::new(MemorySource))
    }
}
#[async_trait]
impl Source for MemorySource {
    async fn serialize_state(&self) -> Result<Vec<u8>, BoxedError> { Ok(vec![]) }
    async fn start(&mut self, _: Sender<(PortHandle, IngestionMessage)>, _: Option<OpIdentifier>) -> Result<(), BoxedError> {
        panic!("baseline construction control must not start ingestion")
    }
}
#[derive(Debug)]
struct MemorySink;
#[async_trait]
impl SinkFactory for MemorySink {
    fn get_input_ports(&self) -> Vec<PortHandle> { vec![DEFAULT_PORT_HANDLE] }
    fn get_input_port_name(&self, _: &PortHandle) -> String { "proof".into() }
    async fn build(&self, _: HashMap<PortHandle, Schema>, _: EventHub) -> Result<Box<dyn Sink>, BoxedError> { Ok(Box::new(MemorySink)) }
    fn prepare(&self, _: HashMap<PortHandle, Schema>) -> Result<(), BoxedError> { Ok(()) }
    fn type_name(&self) -> String { "proof-memory".into() }
}
impl Sink for MemorySink {
    fn process(&mut self, _: TableOperation) -> Result<(), BoxedError> { Ok(()) }
    fn commit(&mut self, _: &Epoch) -> Result<(), BoxedError> { Ok(()) }
    fn on_source_snapshotting_started(&mut self, _: String) -> Result<(), BoxedError> { Ok(()) }
    fn on_source_snapshotting_done(&mut self, _: String, _: Option<OpIdentifier>) -> Result<(), BoxedError> { Ok(()) }
    fn set_source_state(&mut self, _: &[u8]) -> Result<(), BoxedError> { Ok(()) }
    fn get_source_state(&mut self) -> Result<Option<Vec<u8>>, BoxedError> { Ok(None) }
    fn get_latest_op_id(&mut self) -> Result<Option<OpIdentifier>, BoxedError> { Ok(None) }
}
fn construct(sql: &str) -> Result<DagExecutor, dozer_core::errors::ExecutionError> {
    let runtime: Arc<Runtime> = Arc::new(tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap());
    let mut pipeline = AppPipeline::new_with_default_flags();
    let context = statement_to_pipeline(sql, &mut pipeline, Some("results".into()), vec![], runtime.clone()).unwrap();
    let output = context.output_tables_map.get("results").unwrap();
    pipeline.add_sink(Box::new(MemorySink), "proof-sink".into());
    pipeline.connect_nodes(output.node.clone(), output.port, "proof-sink".into(), DEFAULT_PORT_HANDLE);
    let mut sources = AppSourceManager::new();
    sources.add(Box::new(MemorySource), AppSourceMappings::new("proof-memory".into(),
        [("users".into(), 0), ("members".into(), 1)].into_iter().collect())).unwrap();
    let mut app = App::new(sources);
    app.add_pipeline(pipeline);
    let dag = app.into_dag().unwrap();
    runtime.block_on(DagExecutor::new(dag, Default::default()))
}
#[test]
fn original_static_in_builds_real_sql_dag() {
    let sql = "SELECT CustomerID FROM users WHERE CustomerID IN (1, 2)";
    assert!(construct(sql).is_ok(), "unchanged static IN must build");
    println!("ORIGINAL_STATIC_SQL_DAG_PASS: {sql}");
}
#[test]
fn original_dynamic_in_fails_at_real_sql_dag_factory() {
    let sql = "SELECT CustomerID FROM users WHERE CustomerID IN (SELECT CustomerID FROM members)";
    let error = match construct(sql) { Ok(_) => panic!("dynamic IN unexpectedly built on original source"), Err(error) => error };
    match &error {
        dozer_core::errors::ExecutionError::Factory(inner) => match inner.downcast_ref::<Error>() {
            Some(Error::UnsupportedExpression(Expr::InSubquery { .. })) => {},
            other => panic!("unexpected original factory error: {other:?}"),
        },
        other => panic!("unexpected original DAG error: {other:?}"),
    }
    println!("ORIGINAL_DYNAMIC_SQL_DAG_UNSUPPORTED_CONFIRMED: {sql}\n{error:?}");
}
