"""Explicit aggregate leases, fair progress, and PID-driven reclamation.

The cross-process test uses real sockets/pidfds and a filesystem cgroup stand-in.
It proves lease accounting and wakeups, not kernel memory enforcement; the latter
requires the separately granted real public/domain deployment proof.
"""

import hashlib
import json
import os
import select
import shutil
import socket
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests.kit import TempCase, host_values
from tests.project_fixture import make
from unbake import admission, atomic, pool
from unbake.config import Held, Host
from unbake.process import named


class Groups:
    def __init__(self, domain):
        self.domain = domain
        self.live = set()

    def preflight(self):
        return 0

    def check_client(self, pid):
        pass

    def create(self, token, cores, memory):
        path = self.domain.jobs / token
        path.mkdir()
        (path / "cgroup.events").write_text("populated 0\n")
        return path

    def populated(self, path):
        return path in self.live

    def kill(self, path):
        pass  # tests decide when native descendants have actually drained

    def measure(self, path):
        return {"cpu_seconds": None, "memory_peak_bytes": None, "memory_events": {}, "test_double": True}

    def remove(self, path):
        shutil.rmtree(path)


def serve(path):
    domain = admission.Domain.read(Path(path))
    broker = admission.Broker(domain, lambda row: sys.stdout.write(json.dumps(row) + "\n") or None)
    broker.groups = Groups(domain)
    broker.serve()


class AdmissionTests(TempCase):
    def setUp(self):
        super().setUp()
        self.manifest = self.root / "domain.toml"
        self.control = self.root / "control"
        self.jobs = self.root / "jobs"
        self.control.mkdir()
        self.jobs.mkdir()
        self.manifest.write_text(
            f'root = "{self.root}"\ncontrol = "{self.control}"\njobs = "{self.jobs}"\n'
            "cores = 10\nmemory_bytes = 100\ncontrol_cores = 2\ncontrol_memory_bytes = 10\nbypass_limit = 1\n"
        )
        self.domain = admission.Domain.read(self.manifest)
        self.events = []
        self.broker = admission.Broker(self.domain, self.events.append)
        self.broker.groups = Groups(self.domain)
        self.peers = []
        self.children = {}
        self.addCleanup(self.close)
        self.addCleanup(self.save_receipts)

    def close(self):
        for peer in self.peers:
            peer.close()
        for request in list(self.broker.requests):
            request.dead = True
            self.broker.groups.live.clear()
            self.broker.retire(request)

    def request(self, cores, memory):
        server, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.peers.append(peer)
        request = admission.Request(
            server, os.getpid(), os.pidfd_open(os.getpid()), cores, memory, 0.0, f"job{len(self.peers)}"
        )
        self.broker.requests.append(request)
        return request, peer

    def test_cpu_and_memory_are_granted_atomically_and_only_after_native_drain(self):
        first, first_peer = self.request(6, 40)
        _second, second_peer = self.request(2, 50)
        third, third_peer = self.request(1, 1)
        self.broker.dispatch()
        self.assertEqual(json.loads(first_peer.recv(4096))["cores"], 6)
        self.assertEqual(json.loads(second_peer.recv(4096))["memory_bytes"], 50)
        self.assertIsNone(third.group)
        first.dead = True
        self.broker.groups.live.add(first.group)
        self.broker.retire(first)
        self.broker.dispatch()
        self.assertIsNone(third.group, "dead parent cannot release still-running native children")
        self.broker.groups.live.clear()
        self.broker.retire(first)
        self.broker.dispatch()
        self.assertEqual(json.loads(third_peer.recv(4096))["cores"], 1)
        self.assertEqual(sum(r.memory for r in self.broker.requests if r.group), 51)

    def test_smaller_requests_can_progress_but_cannot_starve_an_older_large_request(self):
        active, active_peer = self.request(4, 20)
        self.broker.dispatch()
        active_peer.recv(4096)
        older, older_peer = self.request(6, 20)
        _smaller, smaller_peer = self.request(2, 20)
        last, _last_peer = self.request(2, 20)
        self.broker.dispatch()
        self.assertEqual(json.loads(smaller_peer.recv(4096))["cores"], 2)
        self.assertIsNone(older.group)
        self.assertIsNone(last.group, "bounded overtaking reserves upcoming capacity for older work")
        active.dead = True
        self.broker.retire(active)
        self.broker.dispatch()
        self.assertEqual(json.loads(older_peer.recv(4096))["cores"], 6)
        self.assertIsNone(last.group)

    def test_requests_refuse_bad_values_and_account_for_the_control_reservation(self):
        for cores, memory in ((True, 1), (0, 1), (9, 1), (1, 91), (1, -1)):
            with self.subTest(cores=cores, memory=memory), self.assertRaises(Held):
                self.domain.request(cores, memory)
        self.assertEqual(self.domain.request(8, 90), (8, 90))
        self.assertEqual(replace(self.domain, digest="other").address, self.domain.address)

    def test_broker_bootstrap_uses_a_leaf_below_the_control_slice(self):
        for root, cores, memory in ((self.root, 10, 100), (self.control, 2, 10)):
            (root / "cpu.max").write_text(f"{cores * 100000} 100000")
            (root / "memory.max").write_text(str(memory))
            (root / "memory.swap.max").write_text("0")
        (self.root / "memory.current").write_text("16")
        member = self.root
        write = Path.write_text

        def kernel_write(path, data, *args, **kwargs):
            nonlocal member
            if path.name == "cgroup.procs":
                self.assertNotEqual(path.parent, self.control, "interior control slice cannot host a PID")
                member = path.parent
            return write(path, data.decode() if isinstance(data, bytes) else data, *args, **kwargs)

        with (
            patch.object(admission, "_membership", side_effect=lambda pid: member),
            patch.object(atomic, "control", kernel_write),
        ):
            self.assertEqual(admission.Groups(self.domain).preflight(), 16)
        self.assertEqual(member.parent, self.control)
        self.assertEqual((member / "cgroup.procs").read_text(), str(os.getpid()))

    def test_outside_domain_parent_refuses_before_running_the_command(self):
        values = host_values(self.root)
        values["resources"].update(cores=1, memory_total_bytes=32, memory_parent_bytes=8, memory_worker_bytes=8)
        with (
            self.assertRaisesRegex(Held, "command must start inside"),
            admission.command(Host.from_values(values, "compare")),
        ):
            self.fail("unaccounted command ran")

    def test_pool_and_make_share_the_explicit_command_core_request(self):
        from unbake import build

        values = host_values(self.root)
        values["resources"].update(cores=1, workers=10)
        host = Host.from_values(values, "check")
        self.assertEqual(pool.Pool.from_host(host).size, 1)
        self.assertEqual(pool.describe(host), {"workers": 1})
        self.assertIn("-j1", build.make_command(host, "check"))

    def test_wrong_manifest_refuses_without_charging_capacity(self):
        request, peer = self.request(0, 0)
        self.broker._watch(request.connection.fileno(), "socket", request)
        self.broker._watch(request.pidfd, "pid", request)
        peer.send(json.dumps({"domain": "other", "cores": 1, "memory_bytes": 1}).encode())
        self.broker.packet(request)
        self.assertIn("must match exactly", json.loads(peer.recv(4096))["held"])
        self.assertFalse(self.broker.requests)

    def test_a_second_process_waits_and_owner_death_reclaims_the_first_claim(self):
        # The real service loop receives packets from distinct command PIDs.
        # No game, compiler, large allocation or actual cgroup mutation occurs.
        server = self.spawn(
            [
                sys.executable,
                "-u",
                "-c",
                "from tests.integration.test_admission import serve; import sys; serve(sys.argv[1])",
                str(self.manifest),
            ],
        )
        self.assertEqual(self.line(server)["event"], "resources.ready")
        program = (
            "import json,socket,sys; from pathlib import Path; from unbake.admission import Domain; "
            "d=Domain.read(Path(sys.argv[1])); s=socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET); "
            "s.connect(d.address); "
            "s.send(json.dumps(dict(domain=d.digest,cores=int(sys.argv[2]),memory_bytes=80)).encode()); "
            "print(s.recv(4096).decode(),flush=True); sys.stdin.readline()"
        )
        first = self.spawn([sys.executable, "-u", "-c", program, str(self.manifest), "8"])
        self.assertEqual(self.line(first)["cores"], 8)
        self.assertEqual(self.line(server)["event"], "resources.granted")
        second = self.spawn([sys.executable, "-u", "-c", program, str(self.manifest), "2"])
        self.assertFalse(select.select([second.stdout], [], [], 0.05)[0])
        first.kill()
        first.wait(timeout=2)
        self.assertEqual(self.line(server)["event"], "resources.released")
        self.assertEqual(self.line(second)["cores"], 2)
        self.assertEqual(self.line(server)["event"], "resources.granted")
        self.finish(second, b"\n")
        self.assertEqual(self.line(server)["event"], "resources.released")

    def line(self, process):
        self.assertTrue(select.select([process.stdout], [], [], 3)[0], "process produced no response")
        value = process.stdout.readline()
        self.children[process]["stdout"] += value.decode()
        self.assertTrue(value, "process exited before its protocol response")
        return json.loads(value)

    def spawn(self, argv):
        process = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0
        )
        self.children[process] = {
            "argv": argv,
            "cwd": str(Path.cwd()),
            "pid": process.pid,
            "manifest_sha256": hashlib.sha256(self.manifest.read_bytes()).hexdigest(),
            "manifest": self.manifest.read_text(),
            "cgroup_backend": "test double",
            "stdout": "",
            "stderr": "",
            "exit": None,
        }
        self.addCleanup(self.stop, process)
        return process

    def finish(self, process, data=None):
        if self.children[process]["exit"] is None:
            stdout, stderr = process.communicate(data, timeout=3)
            self.children[process]["stdout"] += stdout.decode()
            self.children[process]["stderr"] += stderr.decode()
            self.children[process]["exit"] = process.returncode

    def stop(self, process):
        if process.poll() is None:
            process.terminate()
        self.finish(process)

    def save_receipts(self):
        if target := os.environ.get("UNBAKE_TEST_RECEIPTS"):
            with Path(target).open("a") as output:
                for record in self.children.values():
                    output.write(json.dumps(record) + "\n")


class AdmissionOrderTests(TempCase):
    def test_admission_precedes_the_writer_lock_and_verb(self):
        import contextlib
        import io

        from unbake.cli import main
        from unbake.cli.output import Result

        project, _ = make(self.root)
        order = []

        @contextlib.contextmanager
        def admitted(host):
            order.append("admitted")
            yield

        @contextlib.contextmanager
        def locked(root, command):
            self.assertEqual(order, ["admitted"])
            order.append("locked")
            yield

        with (
            patch.object(main.config, "load_host", return_value=Host.from_values(host_values(self.root), "publish")),
            patch.object(admission, "command", admitted),
            patch("unbake.lock.project_lock", locked),
            patch.object(main.publish, "run", side_effect=lambda context: Result.ok("publish", {}, [], None)),
        ):
            result = main._run(["--project", str(project.root), "publish", "a.c"], io.StringIO())
        self.assertEqual(result.status, "ok")
        self.assertEqual(order, ["admitted", "locked"])


class StandaloneAdmissionTests(TempCase):
    def setUp(self):
        super().setUp()
        values = host_values(self.root)
        values["resources"]["domain"] = "standalone"
        self.host = Host.from_values(values, "compare")
        self.host.require_command("compare")
        self.addCleanup(admission.receipt.clear)

    def test_standalone_does_not_read_a_manifest_touch_cgroups_or_open_a_socket(self):
        admission.receipt.update(group="stale", measurement_error="stale")
        with (
            patch.object(admission.Domain, "read", side_effect=AssertionError("manifest read")),
            patch.object(admission, "Groups", side_effect=AssertionError("cgroup access")),
            patch.object(admission, "_membership", side_effect=AssertionError("membership read")),
            patch.object(admission.socket, "socket", side_effect=AssertionError("socket opened")),
            patch.object(Path, "write_text", side_effect=AssertionError("cgroup move")),
            admission.command(self.host),
        ):
            self.assertEqual(admission.receipt, {"domain": "standalone", "cores": 4, "memory_bytes": 6_000_000_000})
        self.assertNotIn("group", admission.receipt)

    def test_standalone_pool_and_make_keep_configured_limits(self):
        from unbake import build

        for cores, workers, total, parent, worker, size in (
            (1, 10, 8_000, 1_000, 2_000, 1),
            (12, 2, 8_000, 1_000, 2_000, 2),
            (12, 10, 8_000, 1_000, 2_000, 3),
        ):
            with self.subTest(cores=cores, workers=workers):
                values = {
                    **self.host.values,
                    "resources": {
                        "domain": "standalone",
                        "cores": cores,
                        "workers": workers,
                        "memory_total_bytes": total,
                        "memory_parent_bytes": parent,
                        "memory_worker_bytes": worker,
                    },
                }
                host = Host.from_values(values, "check")
                host.require_command("check")
                with admission.command(host):
                    self.assertEqual(pool.Pool.from_host(host).size, size)
                    self.assertEqual(pool.describe(host), {"workers": size})
                    self.assertIn(f"-j{cores}", build.make_command(host, "check"))

    def test_standalone_preserves_command_failure_and_next_admission_clears_receipt(self):
        with self.assertRaisesRegex(Held, "compare failed"), admission.command(self.host):
            raise Held(named("fixture.refusal", "compare failed", owner="fixture", stage="compare"))
        with admission.command(None):
            self.assertEqual(admission.receipt, {})

    def test_public_compare_reaches_verb_without_broker_or_writer_lock(self):
        import io

        from unbake.cli import main
        from unbake.cli.output import Result

        project, _ = make(self.root)
        with (
            patch.object(main.config, "load_host", return_value=self.host),
            patch.object(admission.Domain, "read", side_effect=AssertionError("manifest read")),
            patch.object(admission.socket, "socket", side_effect=AssertionError("socket opened")),
            patch("unbake.lock.project_lock", side_effect=AssertionError("writer lock")),
            patch.object(main.compare, "run", return_value=Result.ok("compare", {}, [], None)) as run,
        ):
            result = main._run(["--project", str(project.root), "compare", "draft.c"], io.StringIO())
        self.assertEqual(result.status, "ok")
        self.assertEqual(run.call_args.args[0].host.domain, "standalone")

    def test_manifest_mode_still_connects_moves_and_measures(self):
        grant = {
            "domain": "digest",
            "group": str(self.root / "jobs" / "command"),
            "cores": 4,
            "memory_bytes": 6_000_000_000,
        }
        values = {
            **self.host.values,
            "resources": {**self.host.values["resources"], "domain": str(self.root / "domain.toml")},
        }
        host = Host.from_values(values, "compare")
        with (
            patch.object(admission.Domain, "read") as read,
            patch.object(admission.Groups, "check_client") as check,
            patch.object(admission.socket, "socket") as socket_mock,
            patch.object(atomic, "control") as move,
        ):
            domain = read.return_value
            domain.jobs, domain.digest, domain.address = self.root / "jobs", "digest", b"broker"
            connection = socket_mock.return_value.__enter__.return_value
            connection.getsockopt.return_value = admission.struct.pack("3i", os.getpid(), os.getuid(), 0)
            connection.recv.side_effect = [json.dumps(grant).encode(), b'{"cpu_seconds":1.5}']
            with admission.command(host):
                self.assertEqual(admission.receipt, grant)
            read.assert_called_once_with(self.root / "domain.toml")
            domain.request.assert_called_once_with(4, host.memory_total_bytes)
            check.assert_called_once_with(os.getpid())
            connection.connect.assert_called_once_with(b"broker")
            move.assert_called_once_with(self.root / "jobs/command/cgroup.procs", str(os.getpid()))
            self.assertEqual(
                json.loads(connection.send.call_args_list[0].args[0]),
                {
                    "domain": "digest",
                    "cores": 4,
                    "memory_bytes": 6_000_000_000,
                },
            )
            self.assertEqual(admission.receipt["cpu_seconds"], 1.5)
            self.assertEqual(admission.receipt["measurement_scope"], "command-tree-before-result-emission")
