"""Fetch-only audit. No repository/dependency build script or test is executed."""

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
TASK = Path(os.environ["RUNNER_TEMP"]).resolve() / "dozer1659-fetch-r1337"
EVIDENCE = TASK / "evidence"
EVIDENCE.mkdir(parents=True, exist_ok=False)
COMMANDS = []
RESULT = {"status": "STARTED", "phase": "FETCH_ONLY", "started": time.time()}
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


def export_sources(metadata, reached):
    packages = {package["id"]: package for package in metadata["packages"]}
    selected = []
    manifests = []
    members = []
    total = 0
    archive = EVIDENCE / "reached-build-and-proc-macro-sources.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for index, package_id in enumerate(sorted(reached)):
            package = packages[package_id]
            root = Path(package["manifest_path"]).parent.resolve()
            allowed = [REPO.resolve(), (TASK / "cargo").resolve()]
            if not any(root.is_relative_to(base) for base in allowed):
                raise RuntimeError("dependency manifest outside approved acquisition roots")
            prefix = f"p{index:04d}-{package['name']}-{package['version']}"
            execute_targets = [target for target in package["targets"]
                               if "custom-build" in target["kind"] or "proc-macro" in target["kind"]]
            manifests.append({"id": package_id, "name": package["name"], "version": package["version"],
                              "source": package["source"], "features": reached[package_id]["features"],
                              "manifest_sha256": digest(root / "Cargo.toml"),
                              "targets": package["targets"], "executable_compile_targets": execute_targets})
            files = [root / "Cargo.toml"]
            if execute_targets:
                selected.append({"id": package_id, "archive_prefix": prefix,
                                 "reason": "custom-build or proc-macro target"})
                files = []
                for directory, dirs, names in os.walk(root, followlinks=False):
                    dirs[:] = sorted(d for d in dirs if d not in {".git", "target"})
                    for name in dirs:
                        if (Path(directory) / name).is_symlink():
                            raise RuntimeError("symlink directory in dependency source; review manually")
                    files.extend(Path(directory) / name for name in sorted(names))
            for path in files:
                if path.is_symlink() or not path.is_file():
                    raise RuntimeError("non-regular dependency member; review manually")
                relative = path.relative_to(root).as_posix()
                name = prefix + "/" + relative
                size = path.stat().st_size
                total += size
                if total > MAX_SOURCE_BYTES:
                    raise RuntimeError("source audit archive exceeds reviewed 256 MiB limit")
                members.append({"name": name, "bytes": size, "sha256": digest(path), "package_id": package_id})
                tar.add(path, arcname=name, recursive=False)
    save("reached-packages.json", manifests)
    save("executable-compile-source-packages.json", selected)
    save("dependency-source-members.json", members)
    save("dependency-source-archive.json", {"path": archive.name, "bytes": archive.stat().st_size,
         "sha256": digest(archive), "uncompressed_bytes": total, "members": len(members),
         "package_count": len(manifests), "complete_source_package_count": len(selected),
         "exclusions": [".git metadata directories", "target directories"],
         "case_sensitive_archive": True})
    if archive.stat().st_size > MAX_ARCHIVE_BYTES:
        raise RuntimeError("compressed source audit exceeds reviewed 96 MiB limit")


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
    if shutil.disk_usage(TASK).free < 6 * 1024 ** 3:
        raise RuntimeError("less than 6 GiB free before acquisition")
    source_guard("before")
    command("rustup-version", [rustup, "--version"])
    manifest_path = EVIDENCE / "official-rust-manifest.toml"
    download(PINS["rust_manifest_url"], manifest_path, PINS["rust_manifest_sha256"])
    official = tomllib.loads(manifest_path.read_text())
    for name, pin in PINS["rust_components"].items():
        entry = official["pkg"][name]["target"][PINS["target"]]
        if entry["xz_url"] != pin["url"] or entry["xz_hash"] != pin["sha256"]:
            raise RuntimeError("component pin differs from official manifest")
    command("rust-toolchain-install", [rustup, "toolchain", "install", PINS["rust_version"],
            "--profile", "minimal", "--no-self-update"], timeout=600)
    toolchain = TASK / "rustup" / "toolchains" / (PINS["rust_version"] + "-" + PINS["target"])
    installed = tomllib.loads((toolchain / "lib/rustlib/multirust-channel-manifest.toml").read_text())
    for name in PINS["rust_components"]:
        if installed["pkg"][name]["target"][PINS["target"]] != official["pkg"][name]["target"][PINS["target"]]:
            raise RuntimeError("installed Rust manifest differs from pinned component")
    cargo = str(toolchain / "bin/cargo")
    rustc = str(toolchain / "bin/rustc")
    ENV["PATH"] = str(toolchain / "bin") + ":/usr/bin:/bin"
    ENV["RUSTC"] = rustc
    save("toolchain.json", {"rust_version": PINS["rust_version"], "components": PINS["rust_components"],
         "cargo_sha256": digest(Path(cargo)), "rustc_sha256": digest(Path(rustc)),
         "installed_manifest_sha256": digest(toolchain / "lib/rustlib/multirust-channel-manifest.toml")})
    command("cargo-version", [cargo, "--version", "--verbose"])
    command("rustc-version", [rustc, "--version", "--verbose"])
    # Cargo fetch has no package-selection flag: preserve the unchanged workspace lock.
    command("cargo-fetch", [cargo, "fetch", "--locked", "--target", PINS["target"]], timeout=900)
    comparisons = []
    all_reached = set()
    all_packages = {}
    all_nodes = {}
    for package_name, manifest in PINS["selected_packages"].items():
        pair = []
        for mode in ["default", "no-default"]:
            args = [cargo, "metadata", "--format-version", "1", "--locked", "--offline",
                    "--filter-platform", PINS["target"], "--manifest-path", manifest]
            if mode == "no-default":
                args.append("--no-default-features")
            data = json.loads(command("metadata-" + package_name + "-" + mode, args, timeout=180))
            graph = reachable(data, [package_name])
            selected_package = next(p for p in data["packages"] if p["name"] == package_name)
            if "default" in selected_package["features"]:
                raise RuntimeError("selected package now declares defaults; reassess scope")
            pair.append(graph)
            all_reached.update(graph)
            all_packages.update({p["id"]: p for p in data["packages"]})
            merge_export_nodes(all_nodes, data["resolve"]["nodes"])
        equal = pair[0] == pair[1]
        comparisons.append({"package": package_name, "manifest": manifest,
                            "no_declared_default": True, "resolved_root_closure_equal": equal,
                            "closure_packages": len(pair[0])})
        save("default-feature-equivalence.json", comparisons)
        if not equal:
            raise RuntimeError("default and no-default metadata graphs differ; no reduced-scope proof")
    # This graph is conservative: metadata can unify other workspace members' features.
    # Later scoped cargo test/tree receipts establish the actual compiled feature set.
    export_sources({"packages": list(all_packages.values())}, {k: all_nodes[k] for k in all_reached})
    command("locked-feature-tree", [cargo, "tree", "--locked", "--offline", "--target", PINS["target"],
            "-p", "dozer-sql", "-p", "dozer-sql-expression", "-p", "dozer-core", "-e", "normal,build,dev,features"])
    command("locked-feature-tree-no-default", [cargo, "tree", "--locked", "--offline", "--target", PINS["target"],
            "-p", "dozer-sql", "-p", "dozer-sql-expression", "-p", "dozer-core", "-e", "normal,build,dev,features", "--no-default-features"])
    if (EVIDENCE / "locked-feature-tree.stdout").read_bytes() != (EVIDENCE / "locked-feature-tree-no-default.stdout").read_bytes():
        raise RuntimeError("selected-package feature trees differ")
    RESULT.update(status="FETCH_AND_SOURCE_EXPORT_PASS_WAIT_DEPENDENCY_REVIEW",
                  selected_packages=PINS["selected_packages"], feature_trees_equal=True,
                  compiled=False, tests_run=False, repository_or_dependency_build_scripts_run=False,
                  protoc_executed=False, default_graph_limit="metadata workspace feature unification is conservative")


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
    archive = EVIDENCE / "reached-build-and-proc-macro-sources.tar.gz"
    if archive.exists() and archive.stat().st_size > MAX_ARCHIVE_BYTES:
        oversized = {"bytes": archive.stat().st_size, "sha256": digest(archive),
                     "reason": "exceeds 96 MiB; retained outside upload directory on ephemeral runner"}
        archive.rename(TASK / "oversized-source-archive.tar.gz")
        save("oversized-source-archive.json", oversized)
        RESULT["status"] = "FAIL_PRESERVED_NO_RETRY"
    RESULT["finished"] = time.time()
    save("RESULT.json", RESULT)
    records = [{"path": p.relative_to(EVIDENCE).as_posix(), "bytes": p.stat().st_size,
                "sha256": digest(p)} for p in sorted(EVIDENCE.rglob("*")) if p.is_file()]
    save("ARTIFACTS.json", {"files": records, "count": len(records), "bytes": sum(x["bytes"] for x in records)})
    print(json.dumps(RESULT), flush=True)
sys.exit(0 if RESULT["status"] == "FETCH_AND_SOURCE_EXPORT_PASS_WAIT_DEPENDENCY_REVIEW" else 1)
