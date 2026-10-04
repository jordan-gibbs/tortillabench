#!/usr/bin/env python3
"""Run a Blender script on a Daytona cloud GPU and bring back what it wrote.

    python render.py build_tortilla.py [-- script args]   run it in the cloud
    python render.py --status                             list live sandboxes
    python render.py --kill [--all]                       destroy this run's (or every) sandbox

Each call creates one spot GPU sandbox (RTX 5090, else 4090) with Blender
4.5 LTS, uploads the script, and runs

    blender --background --factory-startup --python <script> -- <args>

in /workspace. It streams the log and downloads every file the script
created into the same relative path here. The sandbox is then destroyed
and verified gone, whatever happened, and a hard TTL kills it even if this
process dies. Each call's log goes to cloud_logs/ and a record to cloud_runs.json.

Credentials: DAYTONA_API_KEY (and optionally DAYTONA_ORGANIZATION_ID,
DAYTONA_API_URL, DAYTONA_TARGET) from the environment or a .env file.
The key is never printed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path.cwd()
CONFIG = Path(__file__).resolve().with_name(".tortillabench.json")
CFG = json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}

# Re-run under the interpreter that has the Daytona SDK, if this one doesn't.
try:
    import daytona  # noqa: F401
except ImportError:
    py = CFG.get("python")
    if py and Path(py).exists() and Path(py).resolve() != Path(sys.executable).resolve():
        sys.exit(subprocess.run([py, __file__, *sys.argv[1:]]).returncode)
    sys.exit("render.py: the Daytona SDK is missing. pip install daytona")

from daytona import (CreateSandboxFromImageParams, Daytona, DaytonaConfig, DaytonaError,  # noqa: E402
                     DaytonaNotFoundError, FileUpload, GpuType, Image, ListSandboxesQuery,
                     Resources, SessionExecuteRequest)

MANAGED_KEY, MANAGED_VALUE = "managed-by", "tortillabench"
RUN_KEY = "tortillabench-run"
BLENDER_VERSION = "4.5.13"
BLENDER_URL = f"https://download.blender.org/release/Blender4.5/blender-{BLENDER_VERSION}-linux-x64.tar.xz"
BLENDER_BIN = "/opt/blender/blender"
WORK = "/workspace"
GPU_PREF = ["RTX-5090", "RTX-4090"]
# daytona.io/pricing, on demand (spot bills less, so these are ceilings)
GPU_PER_HOUR = {"RTX-5090": 0.74, "RTX-4090": 0.57}
VCPU_PER_HOUR, GIB_RAM_PER_HOUR = 0.0504, 0.0162
CTRL = "".join(chr(c) for c in range(32))


def say(msg=""):
    print(msg, flush=True)


def load_env() -> dict[str, str]:
    keys = ("DAYTONA_API_KEY", "DAYTONA_ORGANIZATION_ID", "DAYTONA_API_URL", "DAYTONA_TARGET")
    found = {}
    paths = [os.environ.get("DAYTONA_ENV_FILE"), CFG.get("env_file"), HERE / ".env",
             Path(__file__).resolve().with_name(".env")]
    for p in paths:
        if p and Path(p).exists():
            for line in Path(p).read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    found.setdefault(k.strip().removeprefix("export ").strip(), v.strip().strip("\"'"))
    env = {k: os.environ.get(k) or found.get(k, "") for k in keys}
    if not env["DAYTONA_API_KEY"]:
        sys.exit("render.py: DAYTONA_API_KEY not found (environment or .env)")
    return env


def client(env) -> Daytona:
    d = Daytona(DaytonaConfig(api_key=env["DAYTONA_API_KEY"],
                              api_url=env["DAYTONA_API_URL"] or None,
                              target=env["DAYTONA_TARGET"] or None))
    org = env["DAYTONA_ORGANIZATION_ID"]
    if org:
        # Prove the key belongs to the expected org before anything is billed.
        from daytona_api_client import OrganizationsApi
        try:
            OrganizationsApi(d._api_client).get_organization_usage_overview(org)
        except Exception as exc:
            sys.exit(f"render.py: key is not bound to organization {org} ({type(exc).__name__}); refusing")
    return d


def blender_image() -> Image:
    # Same content as colotrail's image, so Daytona's build cache is shared.
    return (Image.debian_slim("3.12")
            .run_commands(
                "apt-get update && apt-get install -y --no-install-recommends "
                "curl ca-certificates xz-utils libx11-6 libxi6 libxxf86vm1 libxfixes3 "
                "libxrender1 libgl1 libegl1 libsm6 libice6 libxkbcommon0 libgomp1 "
                "&& rm -rf /var/lib/apt/lists/*",
                f"curl -fsSL {BLENDER_URL} -o /tmp/blender.tar.xz "
                "&& mkdir -p /opt/blender && tar -xJf /tmp/blender.tar.xz -C /opt/blender "
                "--strip-components=1 && rm /tmp/blender.tar.xz",
                f"{BLENDER_BIN} -b --version",
                f"mkdir -p {WORK} {WORK}/output")
            .workdir(WORK))


def run_label() -> str:
    return CFG.get("run") or re.sub(r"[^a-z0-9._-]+", "-", HERE.name.lower())


def shq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def state(sb) -> str:
    st = getattr(sb, "state", None)
    return str(getattr(st, "value", st) or "unknown")


def destroy(d: Daytona, sb) -> bool:
    """Stop, delete, and verify. True when the sandbox is gone."""
    for force in (False, True):
        try:
            sb.stop(timeout=120, force=force)
            break
        except Exception:
            continue
    try:
        d.delete(sb, timeout=120, wait=True)
    except Exception:
        pass
    try:
        left = d.get(sb.id)
    except DaytonaNotFoundError:
        return True
    return state(left) in ("destroyed", "destroying")


def ours(d: Daytona, run: str | None):
    labels = {MANAGED_KEY: MANAGED_VALUE, **({RUN_KEY: run} if run else {})}
    return [sb for sb in d.list(ListSandboxesQuery(labels=labels))
            if state(sb) not in ("destroyed", "destroying")]


# --- commands --------------------------------------------------------------

def cmd_kill(d: Daytona, everything: bool) -> int:
    run = None if everything else run_label()
    for sb in ours(d, run):
        say(f"killing {sb.id} ({(sb.labels or {}).get(RUN_KEY)}, {state(sb)})")
        destroy(d, sb)
    left = ours(d, run)
    say(f"still alive: {len(left)}" + (f" {[sb.id for sb in left]}" if left else ""))
    return 1 if left else 0


def cmd_status(d: Daytona) -> int:
    live = ours(d, None)
    for sb in live:
        say(f"{sb.id}  {(sb.labels or {}).get(RUN_KEY)}  {state(sb)}")
    say(f"{len(live)} tortillabench sandbox(es) alive")
    return 0


def cmd_run(d: Daytona, a, script_args: list[str]) -> int:
    script = Path(a.script)
    if not script.is_file():
        sys.exit(f"render.py: {script} not found")
    extras = [Path(p) for p in a.with_]
    for p in extras:
        if not p.is_file():
            sys.exit(f"render.py: {p} not found")

    logs = HERE / "cloud_logs"
    logs.mkdir(exist_ok=True)
    n = len(list(logs.glob("*.log"))) + 1
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = logs / f"{n:03d}_{stamp}.log"
    rate = GPU_PER_HOUR[GPU_PREF[0]] + a.cpu * VCPU_PER_HOUR + a.memory * GIB_RAM_PER_HOUR
    say(f"render.py: cloud run {n} — spot {' > '.join(GPU_PREF)}, {a.cpu} vCPU, {a.memory} GB, "
        f"TTL {a.ttl} min (max ${rate * a.ttl / 60:.2f})")

    t0 = time.time()
    try:
        sb = d.create(CreateSandboxFromImageParams(
            name=f"tortilla-{run_label()[:40]}-{n:03d}",
            labels={MANAGED_KEY: MANAGED_VALUE, RUN_KEY: run_label()},
            image=blender_image(), spot=True, ttl_minutes=a.ttl,
            auto_stop_interval=0, auto_delete_interval=0,
            resources=Resources(cpu=a.cpu, memory=a.memory, disk=a.disk, gpu=1,
                                gpu_type=[GpuType(g) for g in GPU_PREF])), timeout=900)
    except DaytonaError as exc:
        if re.search(r"capacit|spot|no available|insufficient|unavailable", str(exc), re.I):
            say(f"render.py: no spot GPU capacity right now. Nothing ran and nothing was billed. "
                f"Wait a few minutes and retry.\n  ({exc})")
            return 75
        raise
    gpu = str(getattr(getattr(sb, "gpu_type", None), "value", getattr(sb, "gpu_type", "")) or "?")
    say(f"render.py: sandbox {sb.id} up on {gpu} ({time.time() - t0:.0f}s)")

    code, files, evicted = None, [], False
    try:
        uploads = [FileUpload(source=str(script), destination=f"{WORK}/{script.name}")]
        uploads += [FileUpload(source=str(p), destination=f"{WORK}/{p.as_posix()}") for p in extras]
        sb.fs.upload_files(uploads, timeout=600)

        args = " ".join(shq(x) for x in script_args)
        inner = (f"cd {WORK} && touch /tmp/.t0 && export PYTHONUNBUFFERED=1 && "
                 f"{BLENDER_BIN} --background --factory-startup --python {shq(script.name)}"
                 + (f" -- {args}" if args else "") +
                 f" 2>&1 | tee /tmp/cloud.log; exit ${{PIPESTATUS[0]}}")
        sb.process.create_session("job")
        cmd = sb.process.execute_session_command(
            "job", SessionExecuteRequest(command="bash -c " + shq(inner), run_async=True))

        seen = 0
        with open(log_path, "w", encoding="utf-8", errors="replace") as log:
            log.write(f"# {stamp} sandbox {sb.id} gpu {gpu}\n# blender --background --factory-startup "
                      f"--python {script.name} {' '.join(script_args)}\n\n")
            while code is None:
                time.sleep(a.poll)
                try:
                    live = d.get(sb.id)
                except DaytonaNotFoundError:
                    live = None
                if live is None or getattr(live, "spot_evicted_at", None) or state(live) in (
                        "destroyed", "destroying", "error", "stopped"):
                    evicted = True
                    say("render.py: sandbox was evicted or died mid-run (spot). Retry the same command.")
                    break
                r = sb.process.get_session_command_logs("job", cmd.cmd_id)
                text = getattr(r, "output", None) or ((r.stdout or "") + (r.stderr or ""))
                for line in text[seen:].splitlines():
                    line = line.lstrip(CTRL).rstrip("\r")
                    print(line, flush=True)
                    log.write(line + "\n")
                seen = len(text)
                code = getattr(sb.process.get_session_command("job", cmd.cmd_id), "exit_code", None)
            log.flush()

            if not evicted:
                listing = sb.process.exec(
                    f"find {WORK} -type f -newer /tmp/.t0 -printf '%s\\t%P\\n'", timeout=120).result
                for row in listing.splitlines():
                    if "\t" not in row:
                        continue
                    size, rel = row.split("\t", 1)
                    dest = HERE / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    sb.fs.download_file(f"{WORK}/{rel}", str(dest), 1800)
                    files.append(rel)
                    say(f"render.py: downloaded {rel} ({int(size) / 1e6:.1f} MB)")
            log.write(f"\n# exit {code}  {time.time() - t0:.0f}s  files: {files}\n")
    except KeyboardInterrupt:
        say("render.py: interrupted")
    finally:
        gone = destroy(d, sb)
        say(f"render.py: sandbox {sb.id} " + ("destroyed (verified)" if gone else "STILL ALIVE — run: python render.py --kill"))

    secs = round(time.time() - t0)
    runs_path = HERE / "cloud_runs.json"
    runs = json.loads(runs_path.read_text(encoding="utf-8")) if runs_path.exists() else []
    runs.append({"n": n, "started": stamp, "sandbox": sb.id, "gpu": gpu, "seconds": secs,
                 "exit_code": code, "evicted": evicted, "args": script_args, "files": files,
                 "log": log_path.relative_to(HERE).as_posix(), "max_cost_usd": round(rate * secs / 3600, 3)})
    runs_path.write_text(json.dumps(runs, indent=2) + "\n", encoding="utf-8")
    say(f"render.py: done — exit {code}, {secs}s, {len(files)} file(s), log {log_path.relative_to(HERE).as_posix()}")
    return 1 if evicted or code is None else code


def main():
    argv = sys.argv[1:]
    script_args = []
    if "--" in argv:
        i = argv.index("--")
        argv, script_args = argv[:i], argv[i + 1:]
    p = argparse.ArgumentParser(description="Run a Blender script on a Daytona cloud GPU")
    p.add_argument("script", nargs="?")
    p.add_argument("--with", dest="with_", action="append", default=[], metavar="FILE",
                   help="extra file to upload next to the script (repeatable)")
    p.add_argument("--cpu", type=int, default=8)
    p.add_argument("--memory", type=int, default=16, help="GB")
    p.add_argument("--disk", type=int, default=20, help="GB")
    p.add_argument("--ttl", type=int, default=60, help="minutes before Daytona kills the sandbox regardless")
    p.add_argument("--poll", type=float, default=5.0)
    p.add_argument("--status", action="store_true")
    p.add_argument("--kill", action="store_true", help="destroy this run's sandboxes")
    p.add_argument("--all", action="store_true", help="with --kill: every tortillabench sandbox")
    a = p.parse_args(argv)

    d = client(load_env())
    if a.kill:
        sys.exit(cmd_kill(d, a.all))
    if a.status:
        sys.exit(cmd_status(d))
    if not a.script:
        p.error("give a script, --status, or --kill")
    sys.exit(cmd_run(d, a, script_args))


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
