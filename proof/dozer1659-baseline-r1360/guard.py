"""Run audited baseline commands in a fresh Linux filesystem/network/PID namespace."""
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import traceback


def validate_empty_ipv4_routes(raw):
    """Accept only kernel empty/no-main-table or valid header-only output."""
    if not isinstance(raw, bytes):
        raise ValueError("route capture must be bytes")
    if raw == b"":
        return "empty-no-header"
    # Linux emits one LF-terminated, optionally space-padded ASCII header.
    # Never treat a malformed one-line payload or whitespace as an empty table.
    if (raw.count(b"\n") != 1 or not raw.endswith(b"\n")
            or any(byte not in (9, 10) and not 32 <= byte <= 126 for byte in raw)):
        raise ValueError("route table contains rows or malformed bytes")
    expected = b"Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT".split()
    if raw[:-1].split() != expected:
        raise ValueError("route table is not a recognized header-only table")
    return "header-only"


def capture_isolation(evidence, stage):
    """Save exact diagnostic bytes before checks, including partial read errors."""
    paths = {"process-status": "/proc/self/status", "interfaces": "/proc/net/dev",
             "ipv4-routes": "/proc/net/route", "ipv6-routes": "/proc/net/ipv6_route",
             "mountinfo": "/proc/self/mountinfo"}
    values = {}
    record = {"stage": stage, "captured": time.time(), "uid": os.getuid(),
              "gid": os.getgid(), "pid": os.getpid(), "files": [], "errors": []}
    def save():
        (evidence / (stage + "-capture.json")).write_text(json.dumps(record, indent=2) + "\n")
    save()
    for name, path in paths.items():
        try:
            raw = Path(path).read_bytes()
            filename = stage + "-" + name + ".raw"
            (evidence / filename).write_bytes(raw)
            values[name] = raw
            record["files"].append({"source": path, "evidence": filename,
                                    "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
        except OSError as error:
            record["errors"].append({"source": path, "error": repr(error)})
        save()
    for name in ["net", "pid", "mnt"]:
        try:
            record[name + "_namespace"] = os.readlink("/proc/self/ns/" + name)
        except OSError as error:
            record["errors"].append({"namespace": name, "error": repr(error)})
        save()
    return values, record


def main(config_path):
    config_path = Path(config_path).resolve()
    config = json.loads(config_path.read_text())
    task = Path(config["task"]).resolve()
    repo = Path(config["repo"]).resolve()
    evidence = task / "evidence"
    rootfs = task / "rootfs"
    _, initial_capture = capture_isolation(evidence, "isolation-before-setup")
    assert not initial_capture["errors"], initial_capture["errors"]
    assert os.geteuid() == 0 and sys.platform == "linux"
    assert task == config_path.parent and repo != task and not repo.is_relative_to(task)
    assert config["uid"] > 0 and config["gid"] > 0
    assert initial_capture["net_namespace"] != config["outer_network_namespace"]
    assert os.getpid() == 1, "expected new PID namespace"
    mounts = []
    def run(*args):
        subprocess.run(args, check=True, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    def bind(origin, destination, readonly=True):
        origin, destination = Path(origin), Path(destination)
        if origin.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.touch(exist_ok=False)
        run("/usr/bin/mount", "--bind", str(origin), str(destination))
        if readonly:
            run("/usr/bin/mount", "-o", "remount,bind,ro,nosuid,nodev", str(destination))
        mounts.append({"source": str(origin), "destination": str(destination.relative_to(rootfs)), "readonly": readonly})
    run("/usr/bin/mount", "--make-rprivate", "/")
    rootfs.mkdir(exist_ok=False)
    # A minimal new root: no runner HOME, Actions credential/command directories,
    # workspace siblings, host /tmp, sockets or user profiles are mounted.
    for path in ["/usr", "/bin", "/lib", "/lib64", "/etc"]:
        origin = Path(path)
        destination = rootfs / path.lstrip("/")
        if origin.is_symlink():
            destination.symlink_to(os.readlink(origin))
            mounts.append({"source": path, "symlink": os.readlink(origin)})
        elif origin.exists():
            bind(origin, destination)
    bind(repo, rootfs / str(repo).lstrip("/"))
    # The task root includes only this run's fresh acquisition/build/evidence data.
    # Non-recursive bind avoids importing the namespace mounts created above.
    bind(task, rootfs / str(task).lstrip("/"), readonly=False)
    for source in [task / "cargo/registry/src", task / "cargo/git/checkouts", task / "rustup", task / "protoc"]:
        assert source.is_dir(), source
        bind(source, rootfs / str(source).lstrip("/"))
    for name in ["null", "zero", "random", "urandom"]:
        destination = rootfs / "dev" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.touch()
        run("/usr/bin/mount", "--bind", "/dev/" + name, str(destination))
        mounts.append({"source": "/dev/" + name, "destination": "dev/" + name, "device": True})
    (rootfs / "proc").mkdir()
    run("/usr/bin/mount", "-t", "proc", "-o", "nosuid,nodev,noexec", "proc", str(rootfs / "proc"))
    (rootfs / "tmp").mkdir(mode=0o700)
    os.chown(rootfs / "tmp", config["uid"], config["gid"])
    os.chroot(rootfs)
    os.chdir(repo)
    os.setgroups([])
    os.setgid(config["gid"])
    os.setuid(config["uid"])
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_NO_NEW_PRIVS)")
    os.environ.clear()
    os.environ.update(config["environment"])
    captured, isolation_capture = capture_isolation(evidence, "isolation-before-checks")
    assert not isolation_capture["errors"], isolation_capture["errors"]
    status = captured["process-status"].decode("ascii")
    assert "NoNewPrivs:\t1" in status
    assert "CapEff:\t0000000000000000" in status
    interfaces = [line.split(":", 1)[0].strip() for line in captured["interfaces"].decode("ascii").splitlines()[2:]]
    assert interfaces == ["lo"], interfaces
    route_representation = validate_empty_ipv4_routes(captured["ipv4-routes"])
    routes = captured["ipv4-routes"].decode("ascii")
    (evidence / "ipv4-route-validation.json").write_text(json.dumps({
        "status": "PASS", "representation": route_representation, "route_rows": 0,
        "bytes": len(captured["ipv4-routes"]),
        "sha256": hashlib.sha256(captured["ipv4-routes"]).hexdigest()}, indent=2) + "\n")
    blocked = None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stream:
            stream.settimeout(2)
            stream.connect(("192.0.2.1", 9))
    except OSError as error:
        blocked = {"errno": error.errno, "message": str(error)}
    (evidence / "external-socket-control.json").write_text(json.dumps({"observed_error": blocked}, indent=2) + "\n")
    assert blocked and blocked["errno"] in {errno.ENETUNREACH, errno.EHOSTUNREACH}, blocked
    readonly_error = None
    try:
        with (repo / ".dozer-proof-write-control").open("xb"):
            pass
    except OSError as error:
        readonly_error = {"errno": error.errno, "message": str(error)}
    (evidence / "checkout-write-control.json").write_text(json.dumps({"observed_error": readonly_error}, indent=2) + "\n")
    assert readonly_error and readonly_error["errno"] == errno.EROFS, readonly_error
    receipt = {"uid": os.getuid(), "gid": os.getgid(), "pid": os.getpid(), "mounts": mounts,
               "network_namespace": os.readlink("/proc/self/ns/net"), "outer_network_namespace": config["outer_network_namespace"],
               "interfaces": interfaces, "ipv4_routes": routes, "ipv4_route_representation": route_representation,
               "ipv6_routes": captured["ipv6-routes"].decode("ascii"),
               "external_socket_control": blocked, "checkout_write_control": readonly_error,
               "process_status": status, "environment": dict(os.environ), "no_new_privileges": True,
               "mountinfo": captured["mountinfo"].decode("utf-8"), "started": time.time()}
    (evidence / "isolation.json").write_text(json.dumps(receipt, indent=2) + "\n")
    records = []
    for item in config["commands"]:
        record = {**item, "started": time.time()}
        records.append(record)
        (evidence / "baseline-commands.json").write_text(json.dumps(records, indent=2) + "\n")
        with (evidence / (item["name"] + ".stdout")).open("wb") as out, (evidence / (item["name"] + ".stderr")).open("wb") as err:
            proc = subprocess.Popen(item["argv"], cwd=repo, env=os.environ.copy(), stdout=out, stderr=err)
            try:
                record["exit_code"] = proc.wait(timeout=item["timeout_seconds"])
            except subprocess.TimeoutExpired:
                record["timed_out"] = True
                proc.kill()
                proc.wait()
                record["exit_code"] = proc.returncode
        record["finished"] = time.time()
        (evidence / "baseline-commands.json").write_text(json.dumps(records, indent=2) + "\n")
        print(item["name"], "exit", record["exit_code"], flush=True)
        if record["exit_code"]:
            raise RuntimeError(item["name"] + " failed; no retry")
        if item["name"] == "protoc-version":
            assert (evidence / "protoc-version.stdout").read_text().strip() == "libprotoc 25.3"
        if item["name"] == "baseline-compile":
            artifacts = []
            for line in (evidence / "baseline-compile.stdout").read_text().splitlines():
                message = json.loads(line)
                if message.get("reason") == "compiler-artifact":
                    pid = message["package_id"]
                    assert pid in config["allowed_features"], pid
                    assert set(message["features"]).issubset(config["allowed_features"][pid]), message
                    artifacts.append(message)
            assert artifacts, "no compiler artifacts"
            (evidence / "compiled-targets.json").write_text(json.dumps(artifacts, indent=2) + "\n")
    (evidence / "isolation-complete.json").write_text(json.dumps({"status": "PASS", "network_namespace": os.readlink("/proc/self/ns/net"), "finished": time.time()}, indent=2) + "\n")


if __name__ == "__main__":
    try:
        main(sys.argv[1])
    except BaseException:
        # Parent command captures this original traceback even if setup fails
        # before the evidence directory is visible inside the new root.
        traceback.print_exc()
        sys.exit(1)
