use std::{collections::HashMap, sync::Arc};

use dozer_core::{
    event::EventHub,
    node::{PortHandle, Processor, ProcessorFactory},
    DEFAULT_PORT_HANDLE,
};
use dozer_types::{
    errors::internal::BoxedError, models::udf_config::UdfConfig, tonic::async_trait, types::Schema,
};
use tokio::runtime::Runtime;

use crate::errors::PipelineError;
use plan::{require_boolean, Plan};
use processor::DynamicSelectionProcessor;

pub(crate) mod plan;
mod processor;

#[derive(Debug)]
pub(crate) struct DynamicSelectionFactory {
    pub id: String,
    pub plan: Plan,
    pub input_count: PortHandle,
    pub udfs: Vec<UdfConfig>,
    pub runtime: Arc<Runtime>,
}

#[async_trait]
impl ProcessorFactory for DynamicSelectionFactory {
    fn id(&self) -> String {
        self.id.clone()
    }
    fn type_name(&self) -> String {
        "DynamicSelection".into()
    }
    fn get_input_ports(&self) -> Vec<PortHandle> {
        (0..=self.input_count).collect()
    }
    fn get_output_ports(&self) -> Vec<PortHandle> {
        vec![DEFAULT_PORT_HANDLE]
    }

    async fn get_output_schema(
        &self,
        output: &PortHandle,
        inputs: &HashMap<PortHandle, Schema>,
    ) -> Result<Schema, BoxedError> {
        if *output != DEFAULT_PORT_HANDLE {
            return Err(PipelineError::InvalidPortHandle(*output).into());
        }
        let schema = inputs
            .get(&DEFAULT_PORT_HANDLE)
            .ok_or(PipelineError::InvalidPortHandle(DEFAULT_PORT_HANDLE))?;
        for port in 1..=self.input_count {
            let inner = inputs
                .get(&port)
                .ok_or(PipelineError::InvalidPortHandle(port))?;
            if inner.fields.len() != 1 {
                return Err(PipelineError::InvalidQuery(
                    "IN subquery must return exactly one column".into(),
                )
                .into());
            }
        }
        Ok(schema.clone())
    }

    async fn build(
        &self,
        inputs: HashMap<PortHandle, Schema>,
        _: HashMap<PortHandle, Schema>,
        _: EventHub,
    ) -> Result<Box<dyn Processor>, BoxedError> {
        let schema = self
            .get_output_schema(&DEFAULT_PORT_HANDLE, &inputs)
            .await?;
        let predicate = self
            .plan
            .compile(&schema, &inputs, &self.udfs, self.runtime.clone())
            .await?;
        require_boolean(&predicate)?;
        Ok(Box::new(DynamicSelectionProcessor::new(
            predicate.predicate,
            schema,
            usize::from(self.input_count),
        )))
    }
}

#[cfg(test)]
mod tests;
