use std::path::Path;

use crate::models::{
    config::Config,
    udf_config::{OnnxConfig, UdfConfig, UdfType, WasmConfig},
};

#[test]
fn standard() {
    let udf_config = r#"
    name: is_fraudulent
    config: !Onnx
      path: ./models/model_file
  "#;
    let deserializer_result = serde_yaml::from_str::<UdfConfig>(udf_config).unwrap();
    let udf_conf = UdfConfig {
        config: UdfType::Onnx(OnnxConfig {
            path: "./models/model_file".to_string(),
        }),
        name: "is_fraudulent".to_string(),
    };
    let expected = udf_conf;
    assert_eq!(expected, deserializer_result);
}

fn config(udfs: &str) -> Result<Config, serde_yaml::Error> {
    serde_yaml::from_str(&format!("version: 1\napp_name: test\n{udfs}"))
}

fn wasm(name: &str, path: &str) -> UdfConfig {
    UdfConfig {
        name: name.to_string(),
        config: UdfType::Wasm(WasmConfig {
            path: path.to_string(),
        }),
    }
}

#[test]
fn original_wasm_mapping() {
    let parsed = config("udfs:\n  wasm:\n    my_function: udfs/wasm/my_function\n").unwrap();
    assert_eq!(
        parsed.udfs,
        vec![wasm("my_function", "udfs/wasm/my_function")]
    );
}

#[test]
fn multiple_wasm_modules_keep_their_names_and_paths() {
    let parsed =
        config("udfs:\n  wasm:\n    second: modules/second.wasm\n    first: modules/first\n")
            .unwrap();
    assert_eq!(
        parsed.udfs,
        vec![
            wasm("first", "modules/first"),
            wasm("second", "modules/second.wasm")
        ]
    );
}

#[test]
fn tagged_list_keeps_existing_kinds_and_accepts_wasm() {
    let parsed = config(
        r#"udfs:
  - name: predict
    config: !Onnx
      path: models/predict
  - name: script
    config: !JavaScript
      module: scripts/transform.js
  - name: my_function
    config: !Wasm
      path: udfs/wasm/my_function
"#,
    )
    .unwrap();
    assert!(matches!(parsed.udfs[0].config, UdfType::Onnx(_)));
    assert!(matches!(parsed.udfs[1].config, UdfType::JavaScript(_)));
    assert_eq!(parsed.udfs[2], wasm("my_function", "udfs/wasm/my_function"));
    let serialized = serde_yaml::to_string(&parsed).unwrap();
    assert_eq!(serde_yaml::from_str::<Config>(&serialized).unwrap(), parsed);
}

#[test]
fn json_list_preserves_the_existing_enum_representation() {
    let parsed: Config = serde_json::from_value(serde_json::json!({
        "version": 1, "app_name": "test", "udfs": [
            {"name": "predict", "config": {"Onnx": {"path": "model.onnx"}}},
            {"name": "script", "config": {"JavaScript": {"module": "script.js"}}},
            {"name": "my_function", "config": {"Wasm": {"path": "module.wasm"}}}
        ]
    }))
    .unwrap();
    assert_eq!(parsed.udfs.len(), 3);
    assert!(matches!(parsed.udfs[0].config, UdfType::Onnx(_)));
    assert!(matches!(parsed.udfs[1].config, UdfType::JavaScript(_)));
    assert_eq!(parsed.udfs[2], wasm("my_function", "module.wasm"));
    let serialized = serde_json::to_string(&parsed).unwrap();
    assert_eq!(serde_json::from_str::<Config>(&serialized).unwrap(), parsed);
}

#[test]
fn json_shorthand_round_trips_through_the_normalized_list() {
    let parsed: Config = serde_json::from_str(
        r#"{
        "version": 1, "app_name": "test", "udfs": {"wasm": {"my_function": "module"}}
    }"#,
    )
    .unwrap();
    assert_eq!(parsed.udfs, vec![wasm("my_function", "module")]);
    let serialized = serde_json::to_value(&parsed).unwrap();
    assert!(serialized["udfs"].is_array());
    assert_eq!(
        serde_json::from_value::<Config>(serialized).unwrap(),
        parsed
    );
}

#[test]
fn absent_and_empty_udfs_preserve_the_default() {
    for input in ["", "udfs: []", "udfs: {}", "udfs: {wasm: {}}"] {
        let parsed = config(input).unwrap();
        assert!(parsed.udfs.is_empty());
        assert!(serde_json::to_value(parsed).unwrap().get("udfs").is_none());
    }
}

#[test]
fn invalid_mapping_shapes_are_not_silently_ignored() {
    for input in [
        "udfs: null",
        "udfs: 1",
        "udfs: text",
        "udfs: {wasm: []}",
        "udfs: {wasm: null}",
        "udfs: {wasm: {f: []}}",
        "udfs: {wasm: {f: {path: module}}}",
        "udfs: {python: {f: module}}",
    ] {
        assert!(config(input).is_err(), "unexpectedly accepted: {input}");
    }
}

#[test]
fn duplicate_wasm_names_are_rejected_in_yaml_and_json() {
    let yaml = config("udfs:\n  wasm:\n    f: first\n    f: second\n").unwrap_err();
    assert!(yaml.to_string().contains("duplicate WASM UDF name"));
    let json = serde_json::from_str::<Config>(
        r#"{
        "version":1,"app_name":"test","udfs":{"wasm":{"f":"first","f":"second"}}
    }"#,
    )
    .unwrap_err();
    assert!(json.to_string().contains("duplicate WASM UDF name"));
}

#[test]
fn duplicate_wasm_sections_are_rejected() {
    assert!(config("udfs:\n  wasm: {first: a}\n  wasm: {second: b}\n").is_err());
    assert!(serde_json::from_str::<Config>(
        r#"{
        "version":1,"app_name":"test","udfs":{"wasm":{"first":"a"},"wasm":{"second":"b"}}
    }"#
    )
    .is_err());
}

#[test]
fn wasm_list_configuration_rejects_missing_or_unknown_fields() {
    for input in [
        "udfs:\n - name: f\n   config: !Wasm {}",
        "udfs:\n - name: f\n   config: !Wasm {path: a, guessed_abi: numeric}",
        "udfs:\n - name: f\n   config: !Unknown {path: a}",
    ] {
        assert!(config(input).is_err(), "unexpectedly accepted: {input}");
    }
}

#[test]
fn module_paths_use_the_project_directory_and_extension() {
    let extensionless = WasmConfig {
        path: "udfs/my_function".to_string(),
    };
    assert_eq!(
        extensionless.module_path(Path::new("project")),
        Path::new("project/udfs/my_function.wasm")
    );
    let explicit = WasmConfig {
        path: "udfs/my_function.wasm".to_string(),
    };
    assert_eq!(
        explicit.module_path(Path::new("project")),
        Path::new("project/udfs/my_function.wasm")
    );
    let absolute = std::env::current_dir().unwrap().join("module.wasm");
    let configured = WasmConfig {
        path: absolute.to_str().unwrap().to_string(),
    };
    assert_eq!(configured.module_path(Path::new("unrelated")), absolute);
}

#[test]
fn binary_list_deserialization_remains_supported() {
    #[derive(crate::serde::Serialize, crate::serde::Deserialize)]
    struct Udfs {
        #[serde(deserialize_with = "crate::models::udf_config::deserialize_udf_configs")]
        udfs: Vec<UdfConfig>,
    }
    let original = Udfs {
        udfs: vec![wasm("f", "module.wasm")],
    };
    let bytes = crate::bincode::serde::encode_to_vec(&original, crate::bincode::config::standard())
        .unwrap();
    let (decoded, consumed): (Udfs, usize) =
        crate::bincode::serde::decode_from_slice(&bytes, crate::bincode::config::standard())
            .unwrap();
    assert_eq!(consumed, bytes.len());
    assert_eq!(decoded.udfs, original.udfs);
}
