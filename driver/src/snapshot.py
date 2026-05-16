"""Desktop snapshot/restore — issue #6 / TRACKING D4.

A snapshot is a `docker commit destiny-desktop destiny-desktop-snapshot:<id>`
plus a metadata JSON row in $STATE_DIR/snapshots.jsonl describing it.

The snapshot captures everything inside the container that's NOT in the
bind-mounted /home/operator volume: installed apt packages, custom apps
the AI dropped into /usr/local, browser binary state outside the home dir,
sway/desktop tweaks. The home volume is shared across snapshots — that
makes restore a fast container swap rather than a tar-and-rsync.

Why this NOT a backup tool: snapshots are point-in-time clones of the
RUNNING container's writable layer. Operators who want full disaster-
recovery (image + volume + audit logs) should use `docker compose down` +
`tar -czf` of the whole state directory. This is "rewind / branch", not
"backup".

Returned snapshot id is `snap_<unix_ms>_<8-hex>` — sortable + unique.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Optional

log = logging.getLogger(__name__)

DESKTOP_CONTAINER = os.environ.get("DESKTOP_CONTAINER", "destiny-desktop")
SNAPSHOT_REPO    = os.environ.get("DESTINY_SNAPSHOT_REPO", "destiny-desktop-snapshot")
SNAPSHOT_TIMEOUT = int(os.environ.get("DESTINY_SNAPSHOT_TIMEOUT_S", "60"))
RESTORE_TIMEOUT  = int(os.environ.get("DESTINY_RESTORE_TIMEOUT_S", "60"))


class SnapshotError(RuntimeError):
    """Anything that goes wrong with the docker plumbing."""


@dataclass
class Snapshot:
    id: str
    tag: str             # full image tag, e.g. "destiny-desktop-snapshot:snap_..."
    created_at: float    # unix ts
    base_container: str
    size_bytes: Optional[int]    # may be None if docker inspect failed
    note: Optional[str] = None   # operator-supplied free-form label

    def to_jsonable(self) -> dict:
        return asdict(self)


def _snapshots_file(state_dir: Path) -> Path:
    return state_dir / "snapshots.jsonl"


def _docker(args: List[str], *, timeout: int) -> subprocess.CompletedProcess:
    """Wrap subprocess.run with a sensible default and DesktopError on failure."""
    try:
        return subprocess.run(
            ["docker"] + args,
            capture_output=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise SnapshotError(f"docker {args[0]} timed out after {timeout}s") from e
    except FileNotFoundError as e:
        raise SnapshotError("docker CLI not found on host") from e


def _image_size_bytes(tag: str) -> Optional[int]:
    """Best-effort. Operators care about disk impact but we don't fail the
    snapshot if `docker image inspect` glitches."""
    try:
        r = _docker(["image", "inspect", "--format", "{{.Size}}", tag], timeout=10)
        if r.returncode != 0:
            return None
        return int(r.stdout.decode("utf-8", "replace").strip())
    except Exception:
        return None


def create_snapshot(state_dir: Path, *, container: Optional[str] = None,
                    note: Optional[str] = None) -> Snapshot:
    """Commit the running desktop container to a tagged image + record metadata.

    Raises SnapshotError if the container isn't running or docker commit fails.
    """
    cn = container or DESKTOP_CONTAINER
    snap_id = f"snap_{int(time.time()*1000)}_{uuid.uuid4().hex[:8]}"
    tag = f"{SNAPSHOT_REPO}:{snap_id}"

    # Confirm the container exists + is running. If not, fail early with
    # a clear message instead of the cryptic "no such container" from
    # docker commit.
    r = _docker(["inspect", "--format", "{{.State.Running}}", cn], timeout=10)
    if r.returncode != 0:
        raise SnapshotError(
            f"desktop container '{cn}' not found "
            f"(docker inspect rc={r.returncode}: {r.stderr.decode('utf-8', 'replace')[:200]})"
        )
    if r.stdout.decode().strip() != "true":
        raise SnapshotError(f"desktop container '{cn}' is not running — can't snapshot")

    r = _docker(["commit", cn, tag], timeout=SNAPSHOT_TIMEOUT)
    if r.returncode != 0:
        raise SnapshotError(
            f"docker commit failed: {r.stderr.decode('utf-8', 'replace')[:300]}"
        )

    snap = Snapshot(
        id=snap_id,
        tag=tag,
        created_at=time.time(),
        base_container=cn,
        size_bytes=_image_size_bytes(tag),
        note=note,
    )
    _append_metadata(state_dir, snap)
    log.info("snapshot created: %s (tag=%s)", snap_id, tag)
    return snap


def _append_metadata(state_dir: Path, snap: Snapshot) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    sf = _snapshots_file(state_dir)
    with sf.open("a") as f:
        f.write(json.dumps(snap.to_jsonable()) + "\n")


def list_snapshots(state_dir: Path) -> List[Snapshot]:
    """Most-recent first. Skips malformed rows (operator hand-edit, etc.)."""
    sf = _snapshots_file(state_dir)
    if not sf.exists():
        return []
    rows: List[Snapshot] = []
    for line in sf.read_text().splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
            rows.append(Snapshot(**d))
        except Exception:
            continue
    rows.sort(key=lambda s: s.created_at, reverse=True)
    return rows


def get_snapshot(state_dir: Path, snapshot_id: str) -> Optional[Snapshot]:
    for s in list_snapshots(state_dir):
        if s.id == snapshot_id:
            return s
    return None


def delete_snapshot(state_dir: Path, snapshot_id: str) -> bool:
    """Remove the docker image tag AND drop the metadata row.

    Refuses to delete the MOST RECENT snapshot — a safeguard so an
    operator can't accidentally orphan their only restore point. To
    delete the most recent, create a fresher snapshot first.

    Returns True if anything was deleted.
    """
    snaps = list_snapshots(state_dir)
    if not snaps:
        return False
    if snaps[0].id == snapshot_id:
        raise SnapshotError(
            "refusing to delete the most-recent snapshot "
            "(create a fresher one first if you really want it gone)"
        )
    target = next((s for s in snaps if s.id == snapshot_id), None)
    if target is None:
        return False

    # Drop the docker image (don't fail the metadata cleanup if the image
    # was already removed manually — operators sometimes prune outside)
    _docker(["image", "rm", target.tag], timeout=20)

    # Rewrite the metadata file without this row
    sf = _snapshots_file(state_dir)
    keep = [s for s in list_snapshots(state_dir) if s.id != snapshot_id]
    if not keep:
        sf.unlink(missing_ok=True)
    else:
        # Re-emit in creation order (oldest first to preserve append-only
        # semantics for tools reading the file as a log)
        keep.sort(key=lambda s: s.created_at)
        sf.write_text("\n".join(json.dumps(s.to_jsonable()) for s in keep) + "\n")
    log.info("snapshot deleted: %s", snapshot_id)
    return True


def restore_snapshot(state_dir: Path, snapshot_id: str, *,
                     container: Optional[str] = None,
                     restart_policy: str = "unless-stopped",
                     extra_run_args: Optional[List[str]] = None) -> Snapshot:
    """Replace the running desktop container with a fresh one from the snapshot tag.

    Steps:
      1. Look up the snapshot record (raise if unknown).
      2. Capture the current container's `docker inspect` to preserve
         port bindings + volume mounts + env (so the new container is
         indistinguishable from a fresh start with the snapshot image).
      3. Stop + rm the current container.
      4. `docker run -d --name <container> ... <snapshot.tag>`.

    The /home/operator bind-mount is preserved by default — restore is
    a "swap the writable layer underneath the home volume" operation.
    If the operator wants a FULL state reset including home, they
    should delete the home volume on the host before calling restore.

    Returns the Snapshot record (for the API response). Raises
    SnapshotError on any docker failure.
    """
    cn = container or DESKTOP_CONTAINER
    snap = get_snapshot(state_dir, snapshot_id)
    if snap is None:
        raise SnapshotError(f"unknown snapshot id: {snapshot_id}")

    # Inspect the current container so we can rebuild from the same
    # config. We need: --publish ports, --volume mounts, --env vars,
    # --network. Use the JSON inspect output.
    r = _docker(["inspect", cn], timeout=10)
    if r.returncode != 0:
        raise SnapshotError(
            f"current container '{cn}' not found — can't preserve config across restore"
        )
    try:
        config = json.loads(r.stdout.decode("utf-8", "replace"))[0]
    except (json.JSONDecodeError, IndexError, KeyError) as e:
        raise SnapshotError(f"failed to parse docker inspect: {e}") from e

    # Stop + rm the current container.
    _docker(["stop", cn], timeout=15)
    _docker(["rm", "-f", cn], timeout=15)

    # Build the new docker run command from the captured config.
    run_args = ["run", "-d", "--name", cn, "--restart", restart_policy]

    # Ports: HostConfig.PortBindings is a dict of "8090/tcp" -> [{"HostIp":"", "HostPort":"8090"}]
    port_bindings = config.get("HostConfig", {}).get("PortBindings") or {}
    for container_port, bindings in port_bindings.items():
        if not bindings:
            continue
        host_port = bindings[0].get("HostPort", "")
        if host_port:
            run_args += ["-p", f"{host_port}:{container_port.split('/')[0]}"]

    # Volumes: HostConfig.Binds is a list of "host:container[:opts]"
    binds = config.get("HostConfig", {}).get("Binds") or []
    for b in binds:
        run_args += ["-v", b]

    # Env: Config.Env is a list of "KEY=VALUE"
    envs = config.get("Config", {}).get("Env") or []
    for e in envs:
        # Skip the auto-set ones docker adds (PATH, HOSTNAME, HOME, etc.)
        # — they'll be re-derived from the new image's defaults
        if any(e.startswith(p) for p in ("PATH=", "HOSTNAME=", "HOME=")):
            continue
        run_args += ["-e", e]

    # Any operator extras (custom --cap-add, --label, etc.)
    if extra_run_args:
        run_args += list(extra_run_args)

    run_args.append(snap.tag)

    r = _docker(run_args, timeout=RESTORE_TIMEOUT)
    if r.returncode != 0:
        raise SnapshotError(
            f"docker run from snapshot failed: {r.stderr.decode('utf-8', 'replace')[:300]}"
        )

    log.info("restored from snapshot %s into container %s", snapshot_id, cn)
    return snap
