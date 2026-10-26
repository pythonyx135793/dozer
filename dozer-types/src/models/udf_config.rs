use std::{
    collections::BTreeMap,
    fmt,
    path::{Path, PathBuf},
};

use schemars::{
    gen::SchemaGenerator,
    schema::{Schema, SchemaObject, SubschemaValidation},
    JsonSchema,
};

use crate::serde::{
    de::{self, value::MapAccessDeserializer, MapAccess, SeqAccess, Visitor},
    Deserialize, Deserializer, Serialize,
};

#[derive(Debug, Serialize, Deserialize, JsonSchema, Eq, PartialEq, Clone)]
#[serde(deny_unknown_fields)]
pub struct UdfConfig {
    /// name of the model function
    pub name: String,
    /// setting for what type of udf to use; Default: Onnx
    pub config: UdfType,
}

#[derive(Debug, Serialize, Deserialize, JsonSchema, Eq, PartialEq, Clone)]
#[serde(deny_unknown_fields)]
pub enum UdfType {
    Onnx(OnnxConfig),
    JavaScript(JavaScriptConfig),
    Wasm(WasmConfig),
}

#[derive(Debug, Serialize, Deserialize, JsonSchema, Eq, PartialEq, Clone)]
#[serde(deny_unknown_fields)]
pub struct OnnxConfig {
    /// path to the model file
    pub path: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct JavaScriptConfig {
    /// path to the module file
    pub module: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct WasmConfig {
    /// Project-relative module path. An extensionless path refers to a .wasm file.
    pub path: String,
}

impl WasmConfig {
    pub fn module_path(&self, project_dir: &Path) -> PathBuf {
        let mut path = PathBuf::from(&self.path);
        if path.extension().is_none() {
            path.set_extension("wasm");
        }
        project_dir.join(path)
    }
}

/// Accept the original UDF list and the issue's `udfs: {wasm: {name: path}}` form.
/// Deserialize list elements directly so YAML tags and JSON enum objects both work.
pub fn deserialize_udf_configs<'de, D>(deserializer: D) -> Result<Vec<UdfConfig>, D::Error>
where
    D: Deserializer<'de>,
{
    struct UdfConfigsVisitor;

    impl<'de> Visitor<'de> for UdfConfigsVisitor {
        type Value = Vec<UdfConfig>;

        fn expecting(&self, formatter: &mut fmt::Formatter) -> fmt::Result {
            formatter.write_str("a UDF list or a map containing wasm function paths")
        }

        fn visit_seq<A>(self, mut sequence: A) -> Result<Self::Value, A::Error>
        where
            A: SeqAccess<'de>,
        {
            let mut configs = Vec::new();
            while let Some(config) = sequence.next_element()? {
                configs.push(config);
            }
            Ok(configs)
        }

        fn visit_map<A>(self, mapping: A) -> Result<Self::Value, A::Error>
        where
            A: MapAccess<'de>,
        {
            let mapping = WasmUdfMapping::deserialize(MapAccessDeserializer::new(mapping))?;
            Ok(mapping
                .wasm
                .0
                .into_iter()
                .map(|(name, path)| UdfConfig {
                    name,
                    config: UdfType::Wasm(WasmConfig { path }),
                })
                .collect())
        }
    }

    if deserializer.is_human_readable() {
        deserializer.deserialize_any(UdfConfigsVisitor)
    } else {
        Vec::<UdfConfig>::deserialize(deserializer)
    }
}

pub fn udf_configs_schema(generator: &mut SchemaGenerator) -> Schema {
    Schema::Object(SchemaObject {
        subschemas: Some(Box::new(SubschemaValidation {
            one_of: Some(vec![
                generator.subschema_for::<Vec<UdfConfig>>(),
                generator.subschema_for::<WasmUdfMapping>(),
            ]),
            ..Default::default()
        })),
        ..Default::default()
    })
}

#[derive(Deserialize, Serialize, JsonSchema)]
#[serde(deny_unknown_fields)]
struct WasmUdfMapping {
    #[serde(default)]
    wasm: WasmFunctionPaths,
}

#[derive(Default, Serialize, JsonSchema)]
#[serde(transparent)]
struct WasmFunctionPaths(BTreeMap<String, String>);

impl<'de> Deserialize<'de> for WasmFunctionPaths {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        struct FunctionPathsVisitor;

        impl<'de> Visitor<'de> for FunctionPathsVisitor {
            type Value = WasmFunctionPaths;

            fn expecting(&self, formatter: &mut fmt::Formatter) -> fmt::Result {
                formatter.write_str("a map of WASM function names to module paths")
            }

            fn visit_map<A>(self, mut mapping: A) -> Result<Self::Value, A::Error>
            where
                A: MapAccess<'de>,
            {
                let mut paths = BTreeMap::new();
                while let Some((name, path)) = mapping.next_entry::<String, String>()? {
                    match paths.entry(name) {
                        std::collections::btree_map::Entry::Vacant(entry) => {
                            entry.insert(path);
                        }
                        std::collections::btree_map::Entry::Occupied(entry) => {
                            return Err(de::Error::custom(format!(
                                "duplicate WASM UDF name: {}",
                                entry.key()
                            )));
                        }
                    }
                }
                Ok(WasmFunctionPaths(paths))
            }
        }

        deserializer.deserialize_map(FunctionPathsVisitor)
    }
}
