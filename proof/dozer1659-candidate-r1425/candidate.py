"""First complete candidate compile, streaming tests and actual timed demo."""

import copy
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import tomllib
import traceback
import urllib.request


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
PINS = json.loads((HERE / "pins.json").read_text())
SOURCE = json.loads((HERE / "source.json").read_text())
TASK = Path(os.environ["RUNNER_TEMP"]).resolve() / "dozer1659-candidate-r1425"
EVIDENCE = TASK / "evidence"
EVIDENCE.mkdir(parents=True, exist_ok=False)
COMMANDS = []
RESULT = {"status": "STARTED", "phase": "DYNAMIC_IN_CANDIDATE", "started": time.time()}
ENV = None
MAX_SOURCE_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_BYTES = 96 * 1024 * 1024


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def save(name, value):
    (EVIDENCE / name).write_text(json.dumps(value, indent=2) + "\n")


def command(name, args, timeout=180, cwd=REPO):
    record = {"name": name, "argv": [str(x) for x in args], "cwd": str(cwd),
              "started": time.time(), "timeout_seconds": timeout}
    COMMANDS.append(record)
    save("commands.json", COMMANDS)
    with (EVIDENCE / (name + ".stdout")).open("wb") as out, \
            (EVIDENCE / (name + ".stderr")).open("wb") as err:
        proc = subprocess.Popen(args, cwd=cwd, env=ENV, stdout=out, stderr=err,
                                start_new_session=True)
        try:
            record["exit_code"] = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            record.update(exit_code=proc.returncode, timed_out=True)
    record["finished"] = time.time()
    save("commands.json", COMMANDS)
    print(name, "exit", record["exit_code"], flush=True)
    if record["exit_code"] != 0:
        raise RuntimeError(f"{name} failed; original stdout/stderr preserved")
    return (EVIDENCE / (name + ".stdout")).read_bytes()


def source_guard(label):
    failures = []
    for entry in SOURCE["files"]:
        path = REPO / entry["path"]
        if not path.is_file() or path.is_symlink():
            failures.append({"path": entry["path"], "problem": "missing or symlink"})
            continue
        if path.stat().st_size != entry["bytes"] or digest(path) != entry["sha256"]:
            failures.append({"path": entry["path"], "problem": "byte/hash mismatch"})
    head = command(label + "-head", ["git", "rev-parse", "HEAD"]).decode().strip()
    parent = command(label + "-parent", ["git", "rev-parse", "HEAD^"]).decode().strip()
    tree = command(label + "-base-tree", ["git", "rev-parse", SOURCE["commit"] + "^{tree}"]).decode().strip()
    delta = command(label + "-delta", ["git", "diff", "--name-only", SOURCE["commit"], "HEAD"]).decode().splitlines()
    status = command(label + "-status", ["git", "status", "--porcelain=v1", "--untracked-files=all"]).decode()
    if parent != SOURCE["commit"] or tree != SOURCE["tree"]:
        failures.append({"problem": "unexpected source ancestry/tree", "parent": parent, "tree": tree})
    if sorted(delta) != sorted(PINS["proof_paths"]) or status:
        failures.append({"problem": "unexpected proof delta or dirty checkout", "delta": delta, "status": status})
    result = {"head": head, "parent": parent, "application_tree": tree,
              "source_files": len(SOURCE["files"]), "source_bytes": sum(x["bytes"] for x in SOURCE["files"]),
              "delta": delta, "failures": failures}
    save(label + "-source.json", result)
    if failures:
        raise RuntimeError("source guard failed")
    return result


def download(url, path, expected):
    record = {"url": url, "path": path.name, "expected_sha256": expected,
              "started": time.time()}
    save("rust-manifest-download.json", record)
    request = urllib.request.Request(url, headers={"User-Agent": "Dozer1659-dependency-audit"})
    with urllib.request.urlopen(request, timeout=60) as response, path.open("xb") as out:
        record["final_url"] = response.url
        if not response.url.startswith("https://static.rust-lang.org/"):
            raise RuntimeError("unexpected Rust manifest redirect")
        shutil.copyfileobj(response, out)
    record.update(bytes=path.stat().st_size, sha256=digest(path), finished=time.time())
    save("rust-manifest-download.json", record)
    if record["sha256"] != expected:
        raise RuntimeError("official Rust manifest changed; do not install")


def reachable(metadata, roots):
    nodes = {node["id"]: node for node in metadata["resolve"]["nodes"]}
    todo = [package["id"] for package in metadata["packages"] if package["name"] in roots]
    if len(todo) != len(roots):
        raise RuntimeError("selected package identity is ambiguous or missing")
    seen = set()
    while todo:
        node_id = todo.pop()
        if node_id in seen:
            continue
        seen.add(node_id)
        todo.extend(dep["pkg"] for dep in nodes[node_id]["deps"])
    return {key: nodes[key] for key in sorted(seen)}


def merge_export_nodes(export_nodes, nodes):
    """Union an independent export inventory without altering raw comparison graphs."""
    for node in nodes:
        merged = copy.deepcopy(node)
        prior = export_nodes.get(node["id"], {})
        merged["features"] = sorted(set(node["features"]) | set(prior.get("features", [])))
        export_nodes[node["id"]] = merged


def verify_installed_rust(official, installed, config, pins):
    """Accept only rustup's added empty arrays; retain exact identity/checksum checks."""
    failures = []
    components = []
    for key in ["manifest-version", "date"]:
        if installed.get(key) != official.get(key):
            failures.append("manifest metadata differs: " + key)
    for name in ["rust", *pins["rust_components"]]:
        expected_version = official.get("pkg", {}).get(name, {}).get("version")
        actual_version = installed.get("pkg", {}).get(name, {}).get("version")
        if not expected_version or actual_version != expected_version:
            failures.append("package version differs: " + name)
    for name in pins["rust_components"]:
        expected = official["pkg"][name]["target"][pins["target"]]
        actual = installed.get("pkg", {}).get(name, {}).get("target", {}).get(pins["target"])
        normalized = copy.deepcopy(actual)
        added_empty = []
        if isinstance(normalized, dict):
            for key in ["components", "extensions"]:
                if key not in expected and normalized.get(key) == []:
                    del normalized[key]
                    added_empty.append(key)
        equal = normalized == expected
        components.append({"name": name, "expected": expected, "actual": actual,
                           "normalized_added_empty_arrays": added_empty, "equal": equal})
        if not equal:
            failures.append("installed target identity/checksum differs: " + name)
    expected_components = {(name, pins["target"]) for name in pins["rust_components"]}
    entries = config.get("components", [])
    actual_components = [(entry.get("pkg"), entry.get("target")) for entry in entries]
    if (config.get("config_version") != "1" or len(actual_components) != len(expected_components)
            or set(actual_components) != expected_components
            or any(entry.get("is_extension", False) is not False for entry in entries)
            or any(set(entry) - {"pkg", "target", "is_extension"} for entry in entries)):
        failures.append("installed components differ from the explicit minimal profile plus pinned rustfmt and clippy")
    return {"status": "PASS" if not failures else "FAIL", "failures": failures,
            "components": components, "installed_config": config,
            "normalization": "Only added empty components/extensions lists; every other target field remains exact."}



AUDITED = json.loads((HERE / "audited-sources.json").read_text())

def main():
    global ENV
    if platform.system() != "Linux" or os.environ.get("GITHUB_REPOSITORY") != PINS["repository"]:
        raise RuntimeError("wrong platform/repository")
    if os.environ.get("GITHUB_EVENT_NAME") != "push" or os.environ.get("GITHUB_REF") != "refs/heads/" + PINS["branch"]:
        raise RuntimeError("wrong trigger/ref")
    # Locate only this runner's supplied rustup; no bootstrap installer or self-update.
    rustup = shutil.which("rustup")
    if not rustup:
        raise RuntimeError("runner rustup unavailable; no automatic substitute")
    for name in ["home", "cargo", "rustup", "tmp", "target"]:
        (TASK / name).mkdir()
    (TASK / "gitconfig").write_text("")
    ENV = {"PATH": "/usr/bin:/bin", "HOME": str(TASK / "home"),
           "CARGO_HOME": str(TASK / "cargo"), "RUSTUP_HOME": str(TASK / "rustup"),
           "TMPDIR": str(TASK / "tmp"), "CARGO_TARGET_DIR": str(TASK / "target"),
           "RUSTUP_DIST_SERVER": "https://static.rust-lang.org", "RUSTUP_TOOLCHAIN": PINS["rust_version"],
           "RUSTUP_AUTO_INSTALL": "0",
           "RUSTUP_MAX_RETRIES": "0", "CARGO_NET_RETRY": "0", "CARGO_HTTP_TIMEOUT": "60",
           "CARGO_TERM_COLOR": "never", "CARGO_REGISTRIES_CRATES_IO_PROTOCOL": "sparse",
           "CARGO_BUILD_JOBS": "2", "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1",
           "GIT_CONFIG_GLOBAL": str(TASK / "gitconfig"), "GIT_CONFIG_SYSTEM": "/dev/null",
           "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
    save("environment.json", {"platform": platform.platform(), "python": sys.version,
         "rustup_path": rustup, "rustup_sha256": digest(Path(rustup)), "child_environment": ENV,
         "github": {key: os.environ.get(key) for key in ["GITHUB_REPOSITORY", "GITHUB_REF", "GITHUB_SHA", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"]},
         "disk": shutil.disk_usage(TASK)._asdict(),
         "network": "dependency acquisition allowed; no OS network isolation claim in fetch-only phase"})
    if shutil.disk_usage(TASK).free < 10 * 1024 ** 3:
        raise RuntimeError("less than 10 GiB free before acquisition")
    source_guard("before")
    manifest_path = EVIDENCE / "official-rust-manifest.toml"
    download(PINS["rust_manifest_url"], manifest_path, PINS["rust_manifest_sha256"])
    official = tomllib.loads(manifest_path.read_text())
    for name, pin in PINS["rust_components"].items():
        entry = official["pkg"][name]["target"][PINS["target"]]
        if entry["xz_url"] != pin["url"] or entry["xz_hash"] != pin["sha256"]:
            raise RuntimeError("component pin differs from official manifest")
    command("rustup-version", [rustup, "--version"])
    toolchains = TASK / "rustup" / "toolchains"
    unexpected = sorted(path.name for path in toolchains.iterdir()) if toolchains.exists() else []
    save("pre-explicit-install.json", {"auto_install": ENV["RUSTUP_AUTO_INSTALL"],
         "toolchains_after_version_query": unexpected, "official_manifest_verified_first": True})
    if unexpected:
        raise RuntimeError("version query unexpectedly installed a toolchain before explicit minimal install")
    command("rust-toolchain-install", [rustup, "toolchain", "install", PINS["rust_version"],
            "--profile", "minimal", "--component", "rustfmt", "--component", "clippy", "--no-self-update"], timeout=600)
    toolchain = TASK / "rustup" / "toolchains" / (PINS["rust_version"] + "-" + PINS["target"])
    captured = []
    for filename in ["multirust-channel-manifest.toml", "multirust-config.toml", "components"]:
        origin = toolchain / "lib/rustlib" / filename
        destination = EVIDENCE / ("installed-" + filename)
        if not origin.is_file() or origin.is_symlink():
            raise RuntimeError("installed metadata is missing or not a regular file: " + filename)
        shutil.copyfile(origin, destination)
        captured.append({"source": str(origin), "evidence": destination.name,
                         "bytes": destination.stat().st_size, "sha256": digest(destination)})
        save("installed-metadata-capture.json", captured)
    installed = tomllib.loads((EVIDENCE / "installed-multirust-channel-manifest.toml").read_text())
    config = tomllib.loads((EVIDENCE / "installed-multirust-config.toml").read_text())
    verification = verify_installed_rust(official, installed, config, PINS)
    save("installed-rust-verification.json", verification)
    if verification["status"] != "PASS":
        raise RuntimeError("installed Rust identity verification failed; exact bytes and differences preserved")
    cargo = str(toolchain / "bin/cargo")
    rustc = str(toolchain / "bin/rustc")
    ENV["PATH"] = str(toolchain / "bin") + ":/usr/bin:/bin"
    ENV["RUSTC"] = rustc
    save("toolchain.json", {"rust_version": PINS["rust_version"], "components": PINS["rust_components"],
         "cargo_sha256": digest(Path(cargo)), "rustc_sha256": digest(Path(rustc)),
         "installed_manifest_sha256": digest(toolchain / "lib/rustlib/multirust-channel-manifest.toml")})
    cargo_version = command("cargo-version", [cargo, "--version", "--verbose"]).decode()
    rustc_version = command("rustc-version", [rustc, "--version", "--verbose"]).decode()
    if (not cargo_version.startswith("cargo " + PINS["rust_version"] + " ")
            or not rustc_version.startswith("rustc " + PINS["rust_version"] + " ")
            or "host: " + PINS["target"] not in rustc_version.splitlines()):
        raise RuntimeError("actual cargo/rustc version or host differs from the pinned toolchain")
    # Cargo fetch has no package-selection flag: preserve the unchanged workspace lock.
    command("cargo-fetch", [cargo, "fetch", "--locked", "--target", PINS["target"]], timeout=900)
    # Fresh-host dependency acquisition is necessary for the first build. Verify
    # it against already reviewed bytes; do not re-export/review identical copies.
    all_packages = {}
    all_nodes = {}
    comparisons = []
    for package_name, manifest in PINS["selected_packages"].items():
        data = json.loads(command("metadata-" + package_name + "-default", [cargo, "metadata", "--format-version", "1", "--locked", "--offline",
                     "--filter-platform", PINS["target"], "--manifest-path", manifest], timeout=180))
        graph = reachable(data, [package_name])
        equal = graph == AUDITED["default_graphs"][package_name]
        comparisons.append({"package": package_name, "ordinary_defaults": True, "audited_graph_equal": equal, "packages": len(graph)})
        save("default-graph-readback.json", comparisons)
        if not equal:
            raise RuntimeError("fresh default graph differs from reviewed source scope; stop before compilation")
        all_packages.update({p["id"]: p for p in data["packages"]})
        merge_export_nodes(all_nodes, graph.values())
    checked_files = 0
    checked_bytes = 0
    for package in AUDITED["all_reached_manifests"]:
        actual = all_packages[package["id"]]
        if any(actual[key] != package[key] for key in ["name", "version", "source"]):
            raise RuntimeError("dependency package identity differs")
        if digest(Path(actual["manifest_path"])) != package["sha256"]:
            raise RuntimeError("reached dependency manifest changed: " + package["name"])
    for package in AUDITED["packages"]:
        actual = all_packages[package["id"]]
        root = Path(actual["manifest_path"]).parent.resolve()
        if not any(root.is_relative_to(base) for base in [REPO.resolve(), (TASK / "cargo").resolve()]):
            raise RuntimeError("audited package outside task/repository")
        expected_names = {entry["path"] for entry in package["files"]}
        actual_names = set()
        for directory, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in {".git", "target"})
            if any((Path(directory) / name).is_symlink() for name in dirs + names):
                raise RuntimeError("symlink in executable source")
            actual_names.update((Path(directory) / name).relative_to(root).as_posix() for name in names)
        # Cargo adds its own extraction markers to registry archives. No other
        # unaudited source member is accepted.
        if actual_names - expected_names - {".cargo-ok", ".cargo-checksum.json"} or expected_names - actual_names:
            raise RuntimeError("complete package file inventory differs: " + package["name"])
        for entry in package["files"]:
            file = root / entry["path"]
            if not file.is_file() or file.stat().st_size != entry["bytes"] or digest(file) != entry["sha256"]:
                raise RuntimeError("audited executable source byte mismatch: " + package["name"] + "/" + entry["path"])
            checked_files += 1
            checked_bytes += entry["bytes"]
    save("dependency-source-readback.json", {"status": "PASS", "complete_packages": len(AUDITED["packages"]),
         "complete_files": checked_files, "complete_bytes": checked_bytes, "reached_manifests": len(AUDITED["all_reached_manifests"]),
         "audited_manifest_sha256": digest(HERE / "audited-sources.json"), "compilation_started": False})
    command("locked-default-feature-tree", [cargo, "tree", "--locked", "--offline", "--target", PINS["target"],
            "-p", "dozer-sql", "-p", "dozer-sql-expression", "-p", "dozer-core", "-e", "normal,build,dev,features"])
    import zipfile
    from pathlib import PurePosixPath
    protoc_archive = EVIDENCE / "protoc-25.3-linux-x86_64.zip"
    request = urllib.request.Request(PINS["protoc"]["url"], headers={"User-Agent": "Dozer1659-baseline"})
    with urllib.request.urlopen(request, timeout=60) as response, protoc_archive.open("xb") as stream:
        shutil.copyfileobj(response, stream)
    if protoc_archive.stat().st_size != PINS["protoc"]["bytes"] or digest(protoc_archive) != PINS["protoc"]["sha256"]:
        raise RuntimeError("official pinned protoc bytes differ")
    protoc_root = TASK / "protoc"
    protoc_root.mkdir()
    members = []
    with zipfile.ZipFile(protoc_archive) as archive:
        if archive.testzip() is not None:
            raise RuntimeError("protoc archive CRC failed")
        names = [entry.filename for entry in archive.infolist()]
        if len(set(names)) != len(names) or len({n.casefold() for n in names}) != len(names):
            raise RuntimeError("duplicate/case-colliding protoc member")
        for entry in archive.infolist():
            path = PurePosixPath(entry.filename)
            if path.is_absolute() or ".." in path.parts or "\\" in entry.filename or ":" in entry.filename or ((entry.external_attr >> 16) & 0o170000) == 0o120000:
                raise RuntimeError("unsafe protoc archive member")
            if entry.is_dir():
                continue
            raw = archive.read(entry)
            destination = protoc_root.joinpath(*path.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(raw)
            members.append({"path": entry.filename, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
    protoc = protoc_root / "bin/protoc"
    protoc.chmod(0o755)
    save("protoc-members.json", {"archive_sha256": digest(protoc_archive), "members": members})
    ENV.update(CARGO_NET_OFFLINE="true", PROTOC=str(protoc), PROTOC_INCLUDE=str(protoc_root / "include"),
               CC="/usr/bin/cc", AR="/usr/bin/ar", RUST_BACKTRACE="1", RUST_TEST_THREADS="2")
    source_guard("pre-build")
    # Preserve the exact application bytes tested, independently of proof files.
    with zipfile.ZipFile(EVIDENCE / "application-source.zip", "x", zipfile.ZIP_DEFLATED) as archive:
        for entry in SOURCE["files"]:
            archive.writestr(entry["path"], (REPO / entry["path"]).read_bytes())
    original = json.loads((HERE / "original-source.json").read_text())
    original_root = TASK / "original-checkout"
    original_root.mkdir()
    # Checkout depth 9: proof plus every exact pinned application ancestor.
    expected_ancestry = PINS["application_ancestry"]
    if len(expected_ancestry) != 8 or expected_ancestry[0] != SOURCE["commit"] or expected_ancestry[-1] != original["commit"]:
        raise RuntimeError("incorrect pinned application ancestry")
    ancestry = []
    for depth, expected in enumerate(expected_ancestry):
        actual = command("application-ancestor-" + str(depth), ["git", "rev-parse", SOURCE["commit"] + "^" * depth]).decode().strip()
        ancestry.append({"depth": depth, "expected": expected, "actual": actual})
        save("application-ancestry.json", ancestry)
        if actual != expected:
            raise RuntimeError("candidate ancestry differs from the exact preserved commits")
    original_tree = command("original-tree", ["git", "rev-parse", original["commit"] + "^{tree}"]).decode().strip()
    if original_tree != original["tree"]:
        raise RuntimeError("original control tree identity differs")
    proc = subprocess.run(["git", "cat-file", "--batch"], input="".join(entry["gitBlob"] + "\n" for entry in original["files"]).encode(),
                          cwd=REPO, env=ENV, capture_output=True, check=True, timeout=30)
    import io
    stream = io.BytesIO(proc.stdout)
    for entry in original["files"]:
        oid, kind, size = stream.readline().decode().split()
        data = stream.read(int(size))
        if stream.read(1) != b"\n" or oid != entry["gitBlob"] or kind != "blob" or len(data) != entry["bytes"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise RuntimeError("original raw Git blob identity differs")
        dest = original_root / entry["path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    if stream.read():
        raise RuntimeError("unexpected bytes after original Git blobs")
    control_files = []
    for name in PINS["candidate"]["original_control_files"]:
        dest = original_root / name
        if dest.exists():
            raise RuntimeError("new regression would overwrite an original source file")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((REPO / name).read_bytes())
        control_files.append({"path": name, "bytes": dest.stat().st_size, "sha256": digest(dest)})
    save("original-control-source.json", {"commit": original["commit"], "tree": original["tree"],
         "original_files": len(original["files"]), "new_public_api_regression_files": control_files})
    args = [cargo, "test", "--locked", "--offline", "--target", PINS["target"], "-p", "dozer-sql", "-p", "dozer-sql-expression", "-p", "dozer-core"]
    demo = TASK / "target" / PINS["target"] / "debug/examples/dynamic_in"
    config = {"task": str(TASK), "repo": str(REPO), "uid": os.getuid(), "gid": os.getgid(),
              "outer_network_namespace": os.readlink("/proc/self/ns/net"), "environment": ENV,
              "extra_readonly_source": str(original_root),
              "allowed_features": {pid: node["features"] for pid, node in all_nodes.items()},
              "commands": [
                  {"name": "protoc-version", "argv": [str(protoc), "--version"], "timeout_seconds": 20},
                  {"name": "cc-version", "argv": ["/usr/bin/cc", "--version"], "timeout_seconds": 20},
                  {"name": "ar-version", "argv": ["/usr/bin/ar", "--version"], "timeout_seconds": 20},
                  {"name": "rustfmt-version", "argv": [str(toolchain / "bin/rustfmt"), "--version"], "timeout_seconds": 20},
                  {"name": "clippy-version", "argv": [str(toolchain / "bin/clippy-driver"), "--version"], "timeout_seconds": 20},
                  {"name": "changed-source-format", "argv": [str(toolchain / "bin/rustfmt"), "--check", "--edition", "2021", "--config", "skip_children=true"] + PINS["candidate"]["changed_rust_paths"], "timeout_seconds": 120},
                  {"name": "candidate-compile", "argv": args + ["--no-run", "--message-format=json"], "timeout_seconds": 1500},
                  {"name": "candidate-tests", "argv": args + ["--", "--nocapture"], "timeout_seconds": 900},
                  {"name": "candidate-clippy", "argv": [cargo, "clippy", "--locked", "--offline", "--target", PINS["target"], "-p", "dozer-sql", "-p", "dozer-sql-expression", "-p", "dozer-core", "--all-targets", "--no-deps", "--message-format=json"], "timeout_seconds": 900},
                  {"name": "demo-build", "argv": [cargo, "build", "--locked", "--offline", "--target", PINS["target"], "-p", "dozer-sql", "--example", "dynamic_in", "--message-format=json"], "timeout_seconds": 300},
                  {"name": "demo-record", "argv": ["/usr/bin/python3", "-B", str(HERE / "record-demo.py"), str(demo), str(EVIDENCE), SOURCE["commit"]], "timeout_seconds": 90},
                  {"name": "original-regression-control", "cwd": str(original_root), "argv": [cargo, "test", "--locked", "--offline", "--target", PINS["target"], "-p", "dozer-sql", "--test", "dynamic_in", PINS["candidate"]["original_control_test"], "--", "--exact", "--nocapture", "--test-threads=1"], "timeout_seconds": 600}]}
    (TASK / "execution.json").write_text(json.dumps(config, indent=2) + "\n")
    save("execution-plan.json", config)
    try:
        command("isolated-candidate", ["/usr/bin/sudo", "-n", "/usr/bin/unshare", "--mount", "--net", "--pid", "--fork",
                "/usr/bin/python3", "-B", str(HERE / "guard.py"), str(TASK / "execution.json")], timeout=3300)
        output = (EVIDENCE / "candidate-tests.stdout").read_text()
        if "test result: ok." not in output or "live_outer_and_inner_changes_reach_the_real_sink ... ok" not in output:
            raise RuntimeError("actual candidate test receipts missing")
        if json.loads((EVIDENCE / "demo-recording.json").read_text())["exit_code"] != 0:
            raise RuntimeError("demo did not complete successfully")
        if json.loads((EVIDENCE / "original-regression-control.json").read_text())["status"] != "EXPECTED_ORIGINAL_UNSUPPORTED":
            raise RuntimeError("new public-API regression did not fail for the original missing feature")
        RESULT.update(status="CANDIDATE_COMPILE_TEST_DEMO_PASS_AWAIT_FINAL_REVIEW", ordinary_defaults=True,
                      selected_packages=PINS["selected_packages"], compiled=True, tests_run=True,
                      recorded_actual_demo=True, video_rendered=False, final_review_complete=False,
                      no_external_network_during_compile_tests=True)
    finally:
        failures = []
        for entry in original["files"] + control_files:
            file = original_root / entry["path"]
            if not file.is_file() or file.stat().st_size != entry["bytes"] or digest(file) != entry["sha256"]:
                failures.append(entry["path"])
        save("original-control-after.json", {"failures": failures, "original_files": len(original["files"]), "regression_files": len(control_files)})
        if failures:
            raise RuntimeError("original control source changed")



try:
    main()
except BaseException as error:
    RESULT.update(status="FAIL_PRESERVED_NO_RETRY", error=str(error))
    (EVIDENCE / "failure.txt").write_text(traceback.format_exc())
finally:
    if ENV is not None:
        try:
            source_guard("after")
        except BaseException:
            RESULT["source_restoration_error"] = traceback.format_exc()
            RESULT["status"] = "FAIL_PRESERVED_NO_RETRY"
    RESULT["finished"] = time.time()
    save("RESULT.json", RESULT)
    records = [{"path": p.relative_to(EVIDENCE).as_posix(), "bytes": p.stat().st_size, "sha256": digest(p)}
               for p in sorted(EVIDENCE.rglob("*")) if p.is_file()]
    save("ARTIFACTS.json", {"files": records, "count": len(records), "bytes": sum(x["bytes"] for x in records)})
    print(json.dumps(RESULT), flush=True)
sys.exit(0 if RESULT["status"] == "CANDIDATE_COMPILE_TEST_DEMO_PASS_AWAIT_FINAL_REVIEW" else 1)
