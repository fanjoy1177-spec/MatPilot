"""Linux process-group supervisor, also launched from Windows through WSL.

Uses only the standard library so the generation environment stays isolated.
"""
import argparse
import os
import signal
import subprocess
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--control-dir", required=True)
    parser.add_argument("--timeout", type=int, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    folder = Path(args.control_dir)
    process = subprocess.Popen(command, start_new_session=True)
    (folder / "linux_process.pid").write_text(str(process.pid))
    start = time.monotonic()
    try:
        while process.poll() is None:
            stopped = (folder / "stop.request").exists()
            if stopped or time.monotonic() - start > args.timeout:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                raise SystemExit(130 if stopped else 124)
            time.sleep(0.25)
        raise SystemExit(process.returncode)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        (folder / "linux_process.exit").write_text(str(process.returncode))


if __name__ == "__main__":
    main()
