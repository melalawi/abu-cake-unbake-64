# Shared resource admission

Every project CLI invocation requires `[resources].domain`, an absolute path to
the same TOML manifest for one machine allocation. There is no unmanaged fallback.
`cores` and `memory_total_bytes` in a host configuration now request one whole
command envelope. They are not a repeated copy of machine capacity. The existing
worker count is a maximum, further bounded by the command's cores and memory.
Make uses the command's granted core count. Native children inherit its cgroup.

The manifest contains exactly these explicit values:

| Key | Meaning |
| --- | --- |
| `root` | Common cgroup v2 domain, containing control and all jobs |
| `control` | Bounded subtree for waiting command parents and the lease service |
| `jobs` | Empty delegated jobs directory, inside the lease service's original cgroup |
| `cores`, `memory_bytes` | Aggregate capacity, matching root's actual kernel limits |
| `control_cores`, `control_memory_bytes` | Reserved control capacity, also matching actual limits |
| `bypass_limit` | Maximum times smaller requests may pass an older queued request |

All numbers are positive integers. Control and jobs must be disjoint descendants
of root. Swap is disabled for the domain/control trees. The broker subtracts its
observed initial memory charges as well as the control reservation: migrating a
PID does not migrate existing memory charges. An oversized request refuses by
name. Manifest disagreement refuses before project work. No values are supplied
by the tool as defaults.

The deployment owner provisions CPU/memory limits and a delegated systemd user
service. A suitable hierarchy is a common `unbake.slice`, a bounded
`unbake-control.slice` below it, and `unbake-resources.service` in the common slice.
The service must use `Delegate=cpu memory` and `KillMode=control-group`, track its
main PID, and kill remaining job descendants on every service exit, including a
SIGKILL of the broker. These are required deployment properties, not inferred
from filesystem writability. The service starts:

```
/absolute/pinned/unbake resources /absolute/resource-domain.toml
```

Set `jobs` to that service's original cgroup plus `/jobs`. After checking domain
limits, the broker moves itself to control, enables the delegated controllers,
and creates the jobs directory. A retained job directory refuses startup; service
recovery must have drained and removed the previous delegated subtree. The
broker does not delete another active service's state. The domain identity owns
one abstract UNIX socket in the shared network namespace; a second broker for
the same physical root refuses binding. All participating jobs must use that
same host/network namespace and source manifest.

Start project commands inside control from process creation, for example through
the deployment's systemd scope launcher in `unbake-control.slice`. Use the exact
immutable tool/dependency path chosen for that invocation. New installations
change the launcher path for new commands; already-running jobs retain their
old input paths. A command started outside control refuses rather than moving
an already-large, unaccounted process or an existing forkserver.

The broker grants CPU and memory together. The command moves itself into its
assigned subgroup before taking its project writer lock, loading project facts,
or starting workers. It creates no replacement executor: Pool and native
adapters continue to run the work. A request cannot retain a parent grant while
waiting for a second child grant. Smaller fitting requests may progress around
a blocked request, with bounded overtaking; once the bound is reached, capacity
is reserved for the older request. That fairness reservation can deliberately
leave capacity idle until the older request fits.

Closing a socket does not release a running command's memory. The broker waits
on the command's pidfd, kills its whole subgroup when that PID exits, and returns
capacity only after the cgroup reports no live descendants. Queue/cancel/drain
events use kernel readiness, without a timer-based permission loop. Grants and
releases are JSONL on the resource service's stdout; command effort adds the
grant, queue time and cgroup CPU/memory measurements. Existing main CPU, worker
CPU, wall, RSS, cache and native fault receipts remain separate. Cgroup memory
includes charged cache/kernel memory and is not labelled as summed process RSS.

Compiler object lifetimes are scoped to each compile consumer. A caller uses
`with runner.compile_unit(...) as object_path:` through the complete place/link
or object-inspection operation. The sole CAS remains keyed by preprocessed text,
arguments and compiler pins. Per-attempt object names preserve the canonical
unit stem for compiler-family selection. They disappear when that consumer ends;
another source for the same unit/version cannot overwrite them. No permanent
`build/work/FUNC/VERSION/FUNC.o` handoff remains.

This admission boundary does not shorten the existing writer command's project
lock. Independent checkouts have independent locks. Same-worktree publication
still requires a separate immutable proof snapshot/readback change before its
compute phase can move outside that lock. Do not claim that this slice proves
arbitrary same-worktree edits and publication can overlap safely.

Deployment acceptance requires a real, small pair of public invocations in
existing independent project/build namespaces: overlapping grants, exact serial
reference outputs, aggregate CPU/memory readback, useful throughput, queue/idle
time, and a failed/cancelled participant whose entire subtree drains while the
other progresses. A socket/pidfd test with simulated cgroup files does not prove
kernel enforcement. Preserve that distinction and the exact source/config/input
receipts. No clone migration or unchanged whole-ROM reassurance run is required.
