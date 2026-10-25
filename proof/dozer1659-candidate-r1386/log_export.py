"""Export the complete fetch audit through ordinary job logs; no network calls."""
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sys
import traceback
import zipfile
import zlib


PREFIX = "DOZER_R1337"
CHUNK_BYTES = 12288
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_MEMBER_BYTES = 512 * 1024 * 1024


def sha(data):
    return hashlib.sha256(data).hexdigest()


def file_sha(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def safe_name(name):
    path = PurePosixPath(name)
    return (not path.is_absolute() and bool(path.parts) and ".." not in path.parts
            and "\\" not in name and ":" not in name and not name.endswith("/"))


def archive_complete_evidence(evidence, destination):
    manifest_path = evidence / "ARTIFACTS.json"
    manifest = json.loads(manifest_path.read_text())
    entries = manifest["files"]
    if manifest["count"] != len(entries) or manifest["bytes"] != sum(e["bytes"] for e in entries):
        raise RuntimeError("evidence manifest counts differ")
    names = [entry["path"] for entry in entries] + ["ARTIFACTS.json"]
    if len(names) != len(set(names)) or not all(safe_name(name) for name in names):
        raise RuntimeError("unsafe or duplicate evidence member")
    actual = []
    for path in evidence.rglob("*"):
        if path.is_symlink():
            raise RuntimeError("symlink in evidence; no traversal")
        if path.is_file():
            actual.append(path.relative_to(evidence).as_posix())
    if set(actual) != set(names):
        raise RuntimeError("evidence directory differs from complete manifest")
    for entry in entries:
        path = evidence / entry["path"]
        if path.stat().st_size != entry["bytes"] or file_sha(path) != entry["sha256"]:
            raise RuntimeError("evidence bytes differ from original manifest: " + entry["path"])
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name in sorted(names):
            archive.write(evidence / name, name)
    identity = {"archive": destination.name, "bytes": destination.stat().st_size,
                "sha256": file_sha(destination), "members": len(names),
                "uncompressed_bytes": sum((evidence / name).stat().st_size for name in names),
                "manifest_sha256": file_sha(manifest_path), "complete_evidence": True}
    # Never trim dependency sources or raw outcomes to fit the log cap.
    if identity["bytes"] > MAX_ARCHIVE_BYTES or identity["uncompressed_bytes"] > MAX_MEMBER_BYTES:
        return identity, False
    with zipfile.ZipFile(destination) as archive:
        if archive.testzip() is not None:
            raise RuntimeError("complete evidence ZIP CRC verification failed")
        for entry in archive.infolist():
            if archive.read(entry) != (evidence / entry.filename).read_bytes():
                raise RuntimeError("complete evidence ZIP readback differs")
    return identity, True


def emit_archive(path, identity, output=sys.stdout):
    size = path.stat().st_size
    if size != identity["bytes"] or size > MAX_ARCHIVE_BYTES or file_sha(path) != identity["sha256"]:
        raise RuntimeError("transport archive size/hash guard failed")
    total = (size + CHUNK_BYTES - 1) // CHUNK_BYTES
    meta = dict(identity, format="zip+base64", chunks=total, raw_bytes_per_chunk_max=CHUNK_BYTES,
                maximum_archive_bytes=MAX_ARCHIVE_BYTES, crc32_per_chunk=True,
                zip_crc_and_byte_readback=True)
    compact = json.dumps(meta, separators=(",", ":"))
    print(PREFIX + "_ARCHIVE_BEGIN " + compact, file=output, flush=True)
    with path.open("rb") as stream:
        for index in range(1, total + 1):
            chunk = stream.read(CHUNK_BYTES)
            encoded = base64.b64encode(chunk).decode("ascii")
            if base64.b64decode(encoded, validate=True) != chunk:
                raise RuntimeError("base64 roundtrip differs")
            crc = format(zlib.crc32(chunk) & 0xffffffff, "08x")
            print(f"{PREFIX}_CHUNK {index:06d}/{total:06d} {len(chunk)} {sha(chunk)} {crc} {encoded}",
                  file=output, flush=True)
        if stream.read(1):
            raise RuntimeError("transport archive grew during export")
    print(PREFIX + "_ARCHIVE_END " + compact, file=output, flush=True)
    return meta


def main():
    task = Path(os.environ["RUNNER_TEMP"]).resolve() / "dozer1659-candidate-r1386"
    task.mkdir(parents=True, exist_ok=True)
    evidence = task / "evidence"
    report = {"status": "TRANSPORT_STARTED", "artifact_upload": False, "cache_upload": False}
    try:
        pins = json.loads((Path(__file__).parent / "pins.json").read_text())
        if (sys.platform != "linux" or os.environ.get("GITHUB_REPOSITORY") != pins["repository"]
                or os.environ.get("GITHUB_EVENT_NAME") != "push"
                or os.environ.get("GITHUB_REF") != "refs/heads/" + pins["branch"]):
            raise RuntimeError("wrong log-export environment")
        expected = {"format": "zip+base64", "prefix": PREFIX, "archive": "observations.zip",
                    "chunk_bytes": CHUNK_BYTES, "maximum_archive_bytes": MAX_ARCHIVE_BYTES,
                    "maximum_decoded_member_bytes": MAX_MEMBER_BYTES,
                    "artifact_upload": False, "cache_upload": False}
        if pins["log_transport"] != expected:
            raise RuntimeError("transport pins differ")
        original = json.loads((evidence / "RESULT.json").read_text())
        report["original_fetch_outcome"] = original
        destination = task / "observations.zip"
        identity, fits = archive_complete_evidence(evidence, destination)
        report["complete_archive"] = identity
        if not fits:
            report.update(status="FAIL_COMPLETE_AUDIT_EXCEEDS_LOG_CAP", log_payload_emitted=False,
                          maximum_archive_bytes=MAX_ARCHIVE_BYTES,
                          meaning="Complete source/evidence retained on ephemeral runner, not transported. No dependency audit or compilation may be claimed.")
            commands = evidence / "commands.json"
            if commands.is_file():
                report["original_commands"] = json.loads(commands.read_text())
            raise RuntimeError("complete audit exceeds practical job-log transport bound; no truncation or alternative service")
        report["transport"] = emit_archive(destination, identity)
        report["status"] = "COMPLETE_AUDIT_LOG_EXPORT_PASS_WAIT_LOCAL_RECONSTRUCTION"
    except BaseException as error:
        if report["status"] == "TRANSPORT_STARTED":
            report["status"] = "FAIL_LOG_EXPORT_PRESERVED"
        report.update(error=str(error), traceback=traceback.format_exc())
    (task / "LOG-TRANSPORT-RESULT.json").write_text(json.dumps(report, indent=2) + "\n")
    print(PREFIX + "_TRANSPORT_RESULT " + json.dumps(report, separators=(",", ":")), flush=True)
    return 0 if report["status"] == "COMPLETE_AUDIT_LOG_EXPORT_PASS_WAIT_LOCAL_RECONSTRUCTION" else 1


if __name__ == "__main__":
    sys.exit(main())
