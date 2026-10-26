"""Verify preparatory configuration only; this is not a runnable WASM UDF proof."""
import base64
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import signal
import subprocess
import sys
import time
import traceback
import urllib.request
import zipfile

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
PINS = json.loads((HERE / "pins.json").read_text())
SOURCE = json.loads((HERE / "source.json").read_text())
TASK = Path(os.environ["RUNNER_TEMP"]) / "dozer1653-config-proof-r2276"
TASK.mkdir(exist_ok=False)
EVIDENCE = TASK / "evidence"
EVIDENCE.mkdir()
ENV = None
COMMANDS = []
RESULT = {"phase": "ABI_NEUTRAL_CONFIGURATION_PREPARATION", "status": "STARTED",
          "no_runtime_or_complete_bounty_claim": True, "started": time.time()}

def sha(raw):
    return hashlib.sha256(raw).hexdigest()

def save(name, value):
    (EVIDENCE / name).write_text(json.dumps(value, indent=2) + "\n")

def command(name, argv, timeout=180):
    record = {"name": name, "argv": [str(x) for x in argv], "started": time.time(),
              "cwd": str(REPO), "timeout_seconds": timeout}
    COMMANDS.append(record)
    save("commands.json", COMMANDS)
    with (EVIDENCE / (name + ".stdout")).open("wb") as out, \
            (EVIDENCE / (name + ".stderr")).open("wb") as err:
        child = subprocess.Popen(argv, cwd=REPO, env=ENV, stdout=out, stderr=err,
                                 start_new_session=True)
        try:
            record["exit_code"] = child.wait(timeout)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
            record.update(exit_code=child.returncode, timed_out=True)
    record["finished"] = time.time()
    save("commands.json", COMMANDS)
    print(name, "exit", record["exit_code"], flush=True)
    if record["exit_code"]:
        raise RuntimeError(name + " failed; exact output retained")
    return (EVIDENCE / (name + ".stdout")).read_bytes()

def source_guard(label):
    actual_paths = command(label + "-git-paths",
                           ["git", "ls-tree", "-rz", "--name-only", PINS["application_commit"]])
    paths = actual_paths.decode().rstrip("\0").split("\0")
    if sorted(paths) != sorted(x["path"] for x in SOURCE["files"]):
        raise RuntimeError("Application source manifest is incomplete")
    failures = []
    for entry in SOURCE["files"]:
        path = REPO / entry["path"]
        if path.is_symlink() or not path.is_file():
            failures.append(entry["path"])
        else:
            raw = path.read_bytes()
            if len(raw) != entry["bytes"] or sha(raw) != entry["sha256"]:
                failures.append(entry["path"])
    result = {"application_commit": PINS["application_commit"], "files": len(paths),
              "bytes": sum(x["bytes"] for x in SOURCE["files"]), "failures": failures}
    save(label + "-source.json", result)
    if failures:
        raise RuntimeError("Application source bytes changed")

def download(url, target, expected):
    request = urllib.request.Request(url, headers={"User-Agent": "Dozer-config-validation"})
    with urllib.request.urlopen(request, timeout=90) as response:
        raw = response.read(96 * 1024 * 1024 + 1)
    if len(raw) > 96 * 1024 * 1024 or sha(raw) != expected:
        raise RuntimeError("Pinned download differs: " + target.name)
    target.write_bytes(raw)
    return raw

def main():
    global ENV
    if os.environ.get("GITHUB_REPOSITORY") != "pythonyx135793/dozer":
        raise RuntimeError("Unexpected repository")
    if os.environ.get("GITHUB_EVENT_NAME") != "push" or \
            os.environ.get("GITHUB_REF") != "refs/heads/" + PINS["branch"]:
        raise RuntimeError("Unexpected event or branch")
    rustup = shutil.which("rustup")
    if not rustup:
        raise RuntimeError("Runner rustup is absent")
    for name in ["home", "cargo", "rustup", "tmp", "target"]:
        (TASK / name).mkdir()
    (TASK / "gitconfig").write_text("")
    ENV = {"PATH": "/usr/bin:/bin", "HOME": str(TASK / "home"),
           "CARGO_HOME": str(TASK / "cargo"), "RUSTUP_HOME": str(TASK / "rustup"),
           "TMPDIR": str(TASK / "tmp"), "CARGO_TARGET_DIR": str(TASK / "target"),
           "RUSTUP_DIST_SERVER": "https://static.rust-lang.org",
           "RUSTUP_TOOLCHAIN": PINS["rust_version"], "RUSTUP_AUTO_INSTALL": "0",
           "RUSTUP_MAX_RETRIES": "0", "CARGO_NET_RETRY": "0", "CARGO_HTTP_TIMEOUT": "60",
           "CARGO_BUILD_JOBS": "2", "CARGO_TERM_COLOR": "never",
           "CARGO_REGISTRIES_CRATES_IO_PROTOCOL": "sparse",
           "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1",
           "GIT_CONFIG_GLOBAL": str(TASK / "gitconfig"), "GIT_CONFIG_SYSTEM": "/dev/null",
           "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "RUST_TEST_THREADS": "2",
           "PYTHONDONTWRITEBYTECODE": "1"}
    source_guard("before")
    download(PINS["rust_manifest_url"], EVIDENCE / "rust-manifest.toml",
             PINS["rust_manifest_sha256"])
    command("rust-install", [rustup, "toolchain", "install", PINS["rust_version"],
                            "--profile", "minimal", "--no-self-update"], 600)
    tools = TASK / "rustup" / "toolchains" / (PINS["rust_version"] + "-" + PINS["target"]) / "bin"
    cargo, rustc = tools / "cargo", tools / "rustc"
    ENV["PATH"] = str(tools) + ":/usr/bin:/bin"
    ENV["RUSTC"] = str(rustc)
    for name, executable in [("cargo", cargo), ("rustc", rustc)]:
        version = command(name + "-version", [str(executable), "--version"]).decode()
        if not version.startswith(name + " " + PINS["rust_version"] + " "):
            raise RuntimeError("Incorrect " + name + " version")
    archive = download(PINS["protoc"]["url"], TASK / "protoc.zip", PINS["protoc"]["sha256"])
    protoc_root = TASK / "protoc"
    protoc_root.mkdir()
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        seen = set()
        for entry in bundle.infolist():
            path = PurePosixPath(entry.filename)
            if path.is_absolute() or ".." in path.parts or "\\" in entry.filename or \
                    ":" in entry.filename or entry.filename.casefold() in seen or \
                    ((entry.external_attr >> 16) & 0o170000) == 0o120000:
                raise RuntimeError("Unsafe protoc archive member")
            seen.add(entry.filename.casefold())
            if entry.is_dir():
                continue
            output = protoc_root.joinpath(*path.parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(bundle.read(entry))
    protoc = protoc_root / "bin/protoc"
    protoc.chmod(0o755)
    ENV.update(PROTOC=str(protoc), PROTOC_INCLUDE=str(protoc_root / "include"))
    command("protoc-version", [str(protoc), "--version"])
    command("cargo-fetch", [str(cargo), "fetch", "--locked", "--target", PINS["target"]], 900)
    ENV["CARGO_NET_OFFLINE"] = "true"
    command("configuration-unit-tests",
            [str(cargo), "test", "--locked", "--offline", "-p", "dozer-types", "--lib"], 1200)
    command("expression-compatibility-check",
            [str(cargo), "check", "--locked", "--offline", "-p", "dozer-sql-expression",
             "--no-default-features"], 1200)
    example = REPO / "dozer-types/examples/config_schema_proof.rs"
    if example.exists():
        raise RuntimeError("Schema proof would overwrite a source file")
    example.parent.mkdir(exist_ok=True)
    helper = ('fn main() { let path = std::env::var_os("DOZER_CONFIG_SCHEMA_OUTPUT").unwrap(); '
              'std::fs::write(path, dozer_types::models::json_schema_helper::get_dozer_schema()'
              '.unwrap()).unwrap(); }\n')
    example.write_text(helper)
    save("schema-helper.json", {"path": str(example.relative_to(REPO)),
                               "sha256": sha(helper.encode()), "application_file": False})
    ENV["DOZER_CONFIG_SCHEMA_OUTPUT"] = str(EVIDENCE / "dozer-schema.generated.json")
    command("schema-generation", [str(cargo), "run", "--locked", "--offline", "-p", "dozer-types",
                                 "--example", "config_schema_proof"], 300)
    source_guard("after")
    RESULT.update(status="SUCCESS", generated_schema_sha256=sha(
        (EVIDENCE / "dozer-schema.generated.json").read_bytes()))

def export():
    # Export only these task evidence files, never the environment, cache or checkout.
    allowed = {p.name for p in EVIDENCE.iterdir()
               if p.name.endswith((".json", ".stdout", ".stderr")) and p.is_file()}
    files = {}
    for name in sorted(allowed):
        raw = (EVIDENCE / name).read_bytes()
        if len(raw) > 8 * 1024 * 1024:
            raise RuntimeError("Evidence file exceeds bounded export")
        files[name] = {"bytes": len(raw), "sha256": sha(raw),
                       "base64": base64.b64encode(raw).decode()}
    raw = json.dumps({"files": files}, separators=(",", ":")).encode()
    encoded = base64.b64encode(gzip.compress(raw, mtime=0)).decode()
    print("DOZER_CONFIG_EVIDENCE_BEGIN", sha(raw), len(raw), flush=True)
    for offset in range(0, len(encoded), 16000):
        print(encoded[offset:offset + 16000], flush=True)
    print("DOZER_CONFIG_EVIDENCE_END", flush=True)

exit_code = 0
try:
    main()
except BaseException as exc:
    exit_code = 1
    RESULT.update(status="FAIL", error=str(exc), traceback=traceback.format_exc())
finally:
    RESULT["finished"] = time.time()
    save("result.json", RESULT)
    export()
sys.exit(exit_code)
