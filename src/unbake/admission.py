"""Whole-command resource leases; Pool and native adapters still execute all work.

Explicit standalone commands use only their host's Pool and native job limits.
For a broker manifest, the deployment owns the cgroup domain. Commands start in
its bounded control subtree, acquire CPU and memory together, then move themselves into their grant
before loading project data or starting a forkserver. Capacity is released only
after the command PID exits and its entire native subtree has drained.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import select
import socket
import struct
import time
import tomllib
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from unbake import atomic
from unbake.config import Held, Host
from unbake.process import capture
from unbake.process import named as cause_named


@dataclass(frozen=True)
class Domain:
    root: Path
    control: Path
    jobs: Path
    cores: int
    memory_bytes: int
    control_cores: int
    control_memory_bytes: int
    bypass_limit: int
    digest: str

    @classmethod
    def read(cls, path: Path) -> Domain:
        try:
            data = path.read_bytes()
            values = tomllib.loads(data.decode())
        except (OSError, ValueError) as error:
            raise Held(
                capture(
                    error,
                    cause=cause_named(
                        "resources.domain", f"resources.domain: {path}: {error}", owner="admission", stage="resources"
                    ),
                )
            ) from error
        paths = {"root", "control", "jobs"}
        integers = {"cores", "memory_bytes", "control_cores", "control_memory_bytes", "bypass_limit"}
        if set(values) != paths | integers:
            raise Held(
                cause_named(
                    "resources.domain",
                    f"resources.domain: expected exactly {', '.join(sorted(paths | integers))}",
                    owner="admission",
                    stage="resources",
                )
            )
        for name in integers:
            if type(values[name]) is not int or values[name] <= 0:
                raise Held(
                    cause_named(
                        f"resources.{name}",
                        f"resources.{name}: expected positive integer",
                        owner="admission",
                        stage="resources",
                    )
                )
        for name in paths:
            if not isinstance(values[name], str) or not Path(values[name]).is_absolute():
                raise Held(
                    cause_named(
                        f"resources.{name}",
                        f"resources.{name}: expected absolute path",
                        owner="admission",
                        stage="resources",
                    )
                )
            values[name] = Path(values[name]).resolve()
        domain = cls(**values, digest=hashlib.sha256(data).hexdigest())
        if domain.control_cores >= domain.cores or domain.control_memory_bytes >= domain.memory_bytes:
            raise Held(
                cause_named(
                    "resources.control",
                    "resources.control: reservation must be smaller than aggregate capacity",
                    owner="admission",
                    stage="resources",
                )
            )
        if (
            domain.control == domain.root
            or domain.jobs == domain.root
            or not domain.control.is_relative_to(domain.root)
            or not domain.jobs.is_relative_to(domain.root)
            or domain.control.is_relative_to(domain.jobs)
            or domain.jobs.is_relative_to(domain.control)
        ):
            raise Held(
                cause_named(
                    "resources.domain",
                    "resources.domain: control and jobs must be disjoint descendants of root",
                    owner="admission",
                    stage="resources",
                )
            )
        return domain

    @property
    def address(self) -> bytes:
        # One kernel-owned socket for this physical cgroup, including aliases
        # of its mount path. No stale socket deletion after a broker crash.
        stat = self.root.stat()
        return f"\0unbake.resources.{stat.st_dev}.{stat.st_ino}".encode()

    def request(self, cores: Any, memory: Any) -> tuple[int, int]:
        for key, value, maximum in (
            ("cores", cores, self.cores - self.control_cores),
            ("memory_total_bytes", memory, self.memory_bytes - self.control_memory_bytes),
        ):
            if type(value) is not int or not 0 < value <= maximum:
                raise Held(
                    cause_named(
                        f"resources.{key}",
                        f"resources.{key}: request must be positive and at most {maximum}",
                        owner="admission",
                        stage="resources",
                    )
                )
        return cores, memory


def _fields(path: Path) -> dict[str, int]:
    return {key: int(value) for key, value in (line.split() for line in path.read_text().splitlines())}


def _membership(pid: int) -> Path:
    for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines():
        if line.startswith("0::"):
            return Path("/sys/fs/cgroup") / line[3:].lstrip("/")
    raise Held(
        cause_named(
            "resources.cgroup",
            "resources.cgroup: unified cgroup v2 membership required",
            owner="admission",
            stage="resources",
        )
    )


class Groups:
    """Only the explicitly delegated jobs subtree is mutable here."""

    def __init__(self, domain: Domain):
        self.domain = domain

    def preflight(self) -> int:
        domain = self.domain
        for root, cores, memory in (
            (domain.root, domain.cores, domain.memory_bytes),
            (domain.control, domain.control_cores, domain.control_memory_bytes),
        ):
            quota, period = (root / "cpu.max").read_text().split()
            if quota == "max" or int(quota) != cores * int(period):
                raise Held(
                    cause_named(
                        "resources.cores",
                        f"resources.cores: {root}/cpu.max disagrees with domain manifest",
                        owner="admission",
                        stage="resources",
                    )
                )
            if (root / "memory.max").read_text().strip() != str(memory):
                raise Held(
                    cause_named(
                        "resources.memory_bytes",
                        f"resources.memory_bytes: {root}/memory.max disagrees with manifest",
                        owner="admission",
                        stage="resources",
                    )
                )
            if (root / "memory.swap.max").read_text().strip() != "0":
                raise Held(
                    cause_named(
                        "resources.memory_bytes",
                        f"resources.memory_bytes: {root}/memory.swap.max must be 0",
                        owner="admission",
                        stage="resources",
                    )
                )
        # The service starts in its own delegated group. Its main PID remains
        # systemd-owned after moving to control; KillMode=control-group still
        # cleans the service's job descendants when that PID exits.
        if _membership(os.getpid()) != domain.jobs.parent:
            raise Held(
                cause_named(
                    "resources.jobs",
                    "resources.jobs: start the broker in the delegated parent of jobs",
                    owner="admission",
                    stage="resources",
                )
            )
        # A slice with controllers enabled cannot contain processes directly.
        # Keep the broker in a leaf alongside the launcher's command scopes.
        for previous in domain.control.glob("unbake-broker-*"):
            if previous.is_dir() and not self.populated(previous):
                previous.rmdir()
        broker = domain.control / f"unbake-broker-{os.getpid()}"
        broker.mkdir()
        atomic.control(broker / "cgroup.procs", str(os.getpid()).encode())
        atomic.control(domain.jobs.parent / "cgroup.subtree_control", b"+cpu +memory")
        domain.jobs.mkdir(exist_ok=True)
        atomic.control(domain.jobs / "cgroup.subtree_control", b"+cpu +memory")
        self.check_client(os.getpid())
        if any(path.is_dir() for path in domain.jobs.iterdir()):
            raise Held(
                cause_named(
                    "resources.jobs",
                    "resources.jobs: retained job subgroups require deployment recovery",
                    owner="admission",
                    stage="resources",
                )
            )
        # Initial broker allocations remain charged to its original group.
        # Reserve them as well as control, rather than assuming migration moved
        # old memory charges. No clients have yet been admitted to jobs.
        return int((domain.jobs.parent / "memory.current").read_text())

    def check_client(self, pid: int) -> None:
        if not _membership(pid).is_relative_to(self.domain.control):
            raise Held(
                cause_named(
                    "resources.domain",
                    "resources.domain: command must start inside the domain control subtree",
                    owner="admission",
                    stage="resources",
                )
            )
        if Path(f"/proc/{pid}/task/{pid}/children").read_text().strip():
            raise Held(
                cause_named(
                    "resources.domain",
                    "resources.domain: acquire before starting children or a forkserver",
                    owner="admission",
                    stage="resources",
                )
            )

    def create(self, token: str, cores: int, memory: int) -> Path:
        path = self.domain.jobs / token
        path.mkdir()
        try:
            for name, value in (
                ("cpu.max", f"{cores * 100000} 100000"),
                ("memory.max", str(memory)),
                ("memory.swap.max", "0"),
                ("memory.oom.group", "1"),
            ):
                atomic.control(path / name, value.encode())
        except BaseException:
            path.rmdir()
            raise
        return path

    def kill(self, path: Path) -> None:
        atomic.control(path / "cgroup.kill", b"1")

    def populated(self, path: Path) -> bool:
        return bool(_fields(path / "cgroup.events")["populated"])

    def measure(self, path: Path) -> dict[str, Any]:
        return {
            "cpu_seconds": _fields(path / "cpu.stat")["usage_usec"] / 1_000_000,
            "memory_peak_bytes": int((path / "memory.peak").read_text()),
            "memory_events": _fields(path / "memory.events"),
        }

    def remove(self, path: Path) -> None:
        path.rmdir()


@dataclass
class Request:
    connection: socket.socket
    pid: int
    pidfd: int
    cores: int
    memory: int
    queued: float
    token: str
    overtaken: int = 0
    granted: float | None = None
    group: Path | None = None
    eventfd: int | None = None
    dead: bool = False


class Broker:
    """A lease queue, never an executor. pidfds and cgroup events own reclamation."""

    def __init__(self, domain: Domain, emit: Callable[[dict[str, Any]], None]):
        self.domain, self.emit = domain, emit
        self.groups = Groups(domain)
        self.poll = select.poll()
        self.requests: list[Request] = []
        self.descriptors: dict[int, tuple[str, Request]] = {}
        self.initial_memory_bytes = 0

    def _watch(self, fd: int, kind: str, request: Request, events: int = select.POLLIN) -> None:
        self.descriptors[fd] = kind, request
        self.poll.register(fd, events | select.POLLERR | select.POLLHUP)

    def _unwatch(self, fd: int) -> None:
        self.descriptors.pop(fd, None)
        self.poll.unregister(fd)

    def accept(self, connection: socket.socket) -> None:
        pid, uid, _gid = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        try:
            # A connection sends one bounded packet. A stalled sender must not
            # block other leases, so accepted sockets remain nonblocking.
            connection.setblocking(False)
            pidfd = os.pidfd_open(pid)
            request = Request(connection, pid, pidfd, 0, 0, time.monotonic(), uuid.uuid4().hex)
            if uid != os.getuid() or any(row.pid == pid for row in self.requests):
                os.close(pidfd)
                raise Held(
                    cause_named(
                        "resources.client",
                        "resources.client: wrong uid or duplicate command PID",
                        owner="admission",
                        stage="resources",
                    )
                )
            self.requests.append(request)
            self._watch(connection.fileno(), "socket", request)
            self._watch(pidfd, "pid", request)
        except (Held, OSError) as error:
            self.refuse(connection, str(error))

    @staticmethod
    def refuse(connection: socket.socket, reason: str) -> None:
        with contextlib.suppress(OSError):
            connection.send(json.dumps({"held": reason}).encode())
        connection.close()

    def packet(self, request: Request) -> None:
        try:
            packet = request.connection.recv(4097)
        except ConnectionError:
            packet = b""
        if not packet:
            self._unwatch(request.connection.fileno())
            request.connection.close()
            if request.group is None:
                self.retire(request)
            return  # a granted claim remains charged until PID exit and subtree drain
        try:
            if len(packet) > 4096:
                raise Held(
                    cause_named(
                        "resources.request",
                        "resources.request: packet exceeds 4096 bytes",
                        owner="admission",
                        stage="resources",
                    )
                )
            values = json.loads(packet)
            if not isinstance(values, dict):
                raise Held(
                    cause_named(
                        "resources.request", "resources.request: expected object", owner="admission", stage="resources"
                    )
                )
            if request.cores:
                if values != {"measure": True} or request.group is None:
                    raise Held(
                        cause_named(
                            "resources.request",
                            "resources.request: already queued or granted",
                            owner="admission",
                            stage="resources",
                        )
                    )
                request.connection.send(json.dumps(self.groups.measure(request.group)).encode())
                return
            if set(values) != {"domain", "cores", "memory_bytes"} or values["domain"] != self.domain.digest:
                raise Held(
                    cause_named(
                        "resources.domain",
                        "resources.domain: request and server manifest must match exactly",
                        owner="admission",
                        stage="resources",
                    )
                )
            request.cores, request.memory = self.domain.request(values["cores"], values["memory_bytes"])
            maximum = self.domain.memory_bytes - self.domain.control_memory_bytes - self.initial_memory_bytes
            if request.memory > maximum:
                raise Held(
                    cause_named(
                        "resources.memory_total_bytes",
                        f"resources.memory_total_bytes: at most {maximum} after broker startup charges",
                        owner="admission",
                        stage="resources",
                    )
                )
            self.groups.check_client(request.pid)
        except (Held, ValueError, TypeError, OSError) as error:
            if request.connection.fileno() >= 0:
                self._unwatch(request.connection.fileno())
            self.refuse(request.connection, str(error))
            if request.group is None:
                self.retire(request)

    def dispatch(self) -> None:
        used_cores = sum(row.cores for row in self.requests if row.group is not None)
        used_memory = sum(row.memory for row in self.requests if row.group is not None)
        waiting = [row for row in self.requests if row.cores and row.group is None]
        for index, request in enumerate(waiting):
            if any(
                row.group is None and row in self.requests and row.overtaken >= self.domain.bypass_limit
                for row in waiting[:index]
            ):
                break  # reserve upcoming releases for the older large request
            if (
                used_cores + request.cores > self.domain.cores - self.domain.control_cores
                or used_memory + request.memory
                > self.domain.memory_bytes - self.domain.control_memory_bytes - self.initial_memory_bytes
            ):
                continue
            try:
                request.group = self.groups.create(request.token, request.cores, request.memory)
                request.eventfd = os.open(request.group / "cgroup.events", os.O_RDONLY)
                self._watch(request.eventfd, "group", request, select.POLLPRI)
                request.granted = time.monotonic()
                grant = {
                    "domain": self.domain.digest,
                    "group": str(request.group),
                    "cores": request.cores,
                    "memory_bytes": request.memory,
                    "queue_seconds": request.granted - request.queued,
                }
                request.connection.send(json.dumps(grant).encode())
            except OSError as error:
                # No complete SEQPACKET grant was sent; the client cannot have
                # entered this group. Refuse just this request, not its siblings.
                if request.connection.fileno() >= 0:
                    self._unwatch(request.connection.fileno())
                self.refuse(request.connection, f"resources.grant: {error}")
                request.dead = True
                self.retire(request)
                continue
            self.emit({"event": "resources.granted", "pid": request.pid, **grant})
            used_cores += request.cores
            used_memory += request.memory
            for older in waiting[:index]:
                if older.group is None:
                    older.overtaken += 1

    def retire(self, request: Request) -> None:
        if request.group is not None:
            if not request.dead or self.groups.populated(request.group):
                return
            self.emit(
                {
                    "event": "resources.released",
                    "pid": request.pid,
                    "domain": self.domain.digest,
                    "group": str(request.group),
                    "cores": request.cores,
                    "memory_bytes": request.memory,
                    "wall_seconds": time.monotonic() - request.granted if request.granted is not None else 0.0,
                    "measurement_scope": "command-tree-until-exit",
                    **self.groups.measure(request.group),
                }
            )
            self.groups.remove(request.group)
        else:
            self.emit(
                {
                    "event": "resources.cancelled",
                    "pid": request.pid,
                    "domain": self.domain.digest,
                    "queue_seconds": time.monotonic() - request.queued,
                }
            )
        for fd in (request.connection.fileno(), request.pidfd, request.eventfd):
            if fd is not None and fd >= 0:
                if fd in self.descriptors:
                    self._unwatch(fd)
                if fd != request.connection.fileno():
                    os.close(fd)
        request.connection.close()
        self.requests.remove(request)

    def serve(self) -> None:
        # Binding the cgroup identity refuses another live broker.
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as listener:
            listener.bind(self.domain.address)
            self.initial_memory_bytes = self.groups.preflight()
            listener.listen()
            listener.setblocking(False)
            self.emit(
                {
                    "event": "resources.ready",
                    "domain": self.domain.digest,
                    "initial_memory_bytes": self.initial_memory_bytes,
                }
            )
            self.poll.register(listener, select.POLLIN)
            try:
                while True:
                    for fd, _events in self.poll.poll():
                        if fd == listener.fileno():
                            connection, _address = listener.accept()
                            self.accept(connection)
                            continue
                        entry = self.descriptors.get(fd)
                        if entry is None:
                            continue
                        kind, request = entry
                        if kind == "socket":
                            try:
                                self.packet(request)
                            except BlockingIOError:
                                continue
                        elif kind == "pid":
                            request.dead = True
                            self._unwatch(request.pidfd)
                            if request.group is not None:
                                self.groups.kill(request.group)
                            self.retire(request)
                        else:
                            os.pread(fd, 4096, 0)  # acknowledge the population event
                            self.retire(request)
                    self.dispatch()
            finally:
                for request in self.requests:
                    if request.group is not None:
                        self.groups.kill(request.group)


receipt: dict[str, Any] = {}


@contextlib.contextmanager
def command(host: Host | None) -> Iterator[None]:
    """A CLI invocation owns its reservation until process exit, including result emission."""
    receipt.clear()
    if host is None:
        yield
        return
    if host.domain == "standalone":
        receipt.update(domain="standalone", cores=host.cores, memory_bytes=host.memory_total_bytes)
        yield
        return
    domain = Domain.read(host.domain)
    domain.request(host.cores, host.memory_total_bytes)
    Groups(domain).check_client(os.getpid())
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as connection:
        try:
            connection.connect(domain.address)
            _pid, uid, _gid = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != os.getuid():
                raise Held(
                    cause_named(
                        "resources.server",
                        "resources.server: domain broker belongs to another uid",
                        owner="admission",
                        stage="resources",
                    )
                )
            connection.send(
                json.dumps(
                    {"domain": domain.digest, "cores": host.cores, "memory_bytes": host.memory_total_bytes}
                ).encode()
            )
            grant = json.loads(connection.recv(4096))
            if "held" in grant:
                raise Held(cause_named("admission.command", str(grant["held"]), owner="admission", stage="resources"))
            group = Path(grant["group"])
            if group.parent != domain.jobs or grant["domain"] != domain.digest:
                raise Held(
                    cause_named(
                        "resources.grant",
                        "resources.grant: wrong domain or job subgroup",
                        owner="admission",
                        stage="resources",
                    )
                )
            atomic.control(group / "cgroup.procs", str(os.getpid()).encode())
            receipt.update(grant)
        except (OSError, ValueError, KeyError) as error:
            raise Held(
                capture(
                    error,
                    cause=cause_named(
                        "resources.admission", f"resources.admission: {error}", owner="admission", stage="resources"
                    ),
                )
            ) from error
        failed = False
        try:
            yield
        except BaseException:
            failed = True
            raise
        finally:
            # Closing the connection does not release this process's memory.
            # The broker's pidfd owns release, after final stdout and process exit.
            try:
                connection.send(b'{"measure":true}')
                measured = json.loads(connection.recv(4096))
                if "held" in measured:
                    raise ValueError(measured["held"])
                receipt.update(measured)
                receipt["measurement_scope"] = "command-tree-before-result-emission"
            except (OSError, ValueError) as error:
                receipt["measurement_error"] = str(error)
                if not failed:
                    raise Held(
                        capture(
                            error,
                            cause=cause_named(
                                "resources.measurement",
                                f"resources.measurement: {error}",
                                owner="admission",
                                stage="resources",
                            ),
                        )
                    ) from error
