"""Record real Linux example output and timing; no terminal output is synthesized."""
import codecs
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time
import traceback


def main():
    binary, destination, commit = sys.argv[1:]
    binary = Path(binary).resolve(strict=True)
    evidence = Path(destination).resolve(strict=True)
    if sys.platform != "linux" or not binary.is_file() or binary.is_symlink():
        raise RuntimeError("expected the actual compiled Linux example")
    started = time.time()
    clock = time.monotonic()
    report = {"status": "STARTED", "argv": [str(binary)], "cwd": str(Path.cwd()),
              "source_commit": commit, "binary_bytes": binary.stat().st_size,
              "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
              "started": started, "capture": "actual child stdout/stderr pipes with monotonic receipt times",
              "terminal_emulation": False, "video_rendered": False}
    (evidence / "demo-recording.json").write_text(json.dumps(report, indent=2) + "\n")
    raw = {name: bytearray() for name in ["stdout", "stderr"]}
    decoders = {name: codecs.getincrementaldecoder("utf-8")("strict") for name in raw}
    receipts = []
    proc = None
    try:
        with (evidence / "demo.cast").open("x", encoding="utf-8", newline="\n") as cast:
            cast.write(json.dumps({"version": 2, "width": 128, "height": 38,
                                  "timestamp": int(started), "title": "Actual Linux Dozer streaming IN / NOT IN",
                                  "env": {"TERM": "dumb", "SHELL": "none"},
                                  "source_commit": commit, "capture": report["capture"]}) + "\n")
            cast.flush()
            proc = subprocess.Popen([str(binary)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    env=os.environ.copy(), start_new_session=True)
            with selectors.DefaultSelector() as selector:
                for name in raw:
                    selector.register(getattr(proc, name), selectors.EVENT_READ, name)
                while selector.get_map():
                    if time.monotonic() - clock > 75:
                        report["timed_out"] = True
                        raise TimeoutError("actual demo exceeded 75 seconds")
                    for key, _ in selector.select(timeout=0.25):
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        name = key.data
                        when = time.monotonic() - clock
                        if chunk:
                            offset = len(raw[name])
                            raw[name].extend(chunk)
                            receipts.append({"seconds": when, "stream": name, "offset": offset,
                                             "bytes": len(chunk), "sha256": hashlib.sha256(chunk).hexdigest()})
                            if sum(map(len, raw.values())) > 16 * 1024 * 1024:
                                raise RuntimeError("actual demo output exceeded capture bound")
                        else:
                            selector.unregister(key.fileobj)
                        text = decoders[name].decode(chunk, final=not chunk)
                        if text:
                            cast.write(json.dumps([when, "o", text], ensure_ascii=False) + "\n")
                            cast.flush()
                report["exit_code"] = proc.wait(timeout=5)
        stdout = bytes(raw["stdout"]).decode("utf-8")
        if report["exit_code"] != 0:
            raise RuntimeError("actual demo exited unsuccessfully")
        if stdout.count("DEMO COMPLETE: every real committed result matched its asserted multiset.") != 1:
            raise RuntimeError("actual example completion marker missing or duplicated")
        if stdout.count("\nSTEP ") != 14 or stdout.count("ASSERTIONS: PASS (") != 14:
            raise RuntimeError("actual example did not report all 14 committed transitions")
        report.update(status="ACTUAL_LINUX_RECORDING_PASS", committed_transitions=14)
    except BaseException as error:
        report.update(status="FAIL_PRESERVED", error=str(error), traceback=traceback.format_exc())
        if proc is not None and proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        if proc is not None:
            report["exit_code"] = proc.returncode
    finally:
        for name, data in raw.items():
            (evidence / ("demo." + name)).write_bytes(data)
            report[name] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        report.update(finished=time.time(), elapsed_seconds=time.monotonic() - clock, chunks=receipts)
        (evidence / "demo-recording.json").write_text(json.dumps(report, indent=2) + "\n")
    print(report["status"], "actual output retained in demo.stdout, demo.stderr and demo.cast", flush=True)
    return 0 if report["status"] == "ACTUAL_LINUX_RECORDING_PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
