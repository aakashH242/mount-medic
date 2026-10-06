# Linux diagnosis and manual repair without installing Mount Medic

Use available Linux tools. Nothing in this route requires Mount Medic, its source code, a desktop session, or a system service. Installing a missing utility remains a separate user decision; if installation is declined, collect what is available and state what cannot be checked.

## Collect evidence

Run the bundled script with Python 3.8+ from any working directory, using its absolute path:

```sh
python3 /path/to/mount-medic/scripts/inspect_volume.py
python3 /path/to/mount-medic/scripts/inspect_volume.py /dev/disk/by-id/SELECTED-PARTITION
```

The first command lists devices. After identifying the user's target, the second gathers that device's dependency tree and matching mounts. It returns JSON, uses only `lsblk` and `findmnt`, does not request root, and does not open a device for writing. Exit 0 means the queries completed, **not** that the filesystem is healthy or safe to repair; exit 2 means at least one query or argument failed. Missing identity fields remain unknown. Output may include parent-device metadata; avoid sharing device identifiers unnecessarily.

If Python is unavailable, run the native queries directly; no script installation is needed:

```sh
lsblk --json --bytes --paths --tree --output NAME,TYPE,MAJ:MIN,SIZE,FSTYPE,LABEL,UUID,PARTUUID,MODEL,SERIAL,RO,MOUNTPOINTS
findmnt --kernel --json --output SOURCE,TARGET,FSTYPE,OPTIONS,MAJ:MIN
```

Match the intended partition using model/serial, size, filesystem UUID, partition UUID, and layout. Resolve duplicate labels with the user. Prefer a verified `/dev/disk/by-id/…-partN` link for a partition; never invent that name. Recheck the resolved device immediately before a later operation. A stable-looking link is not protection against replacement or a changed partition table.

These queries do not perform an NTFS integrity check. `findmnt` sees the current process's mount namespace; no matching mount does not prove the device is unused by containers, other namespaces, or raw-device users. Stop writes if you cannot establish exclusive use. For unfamiliar RAID, device-mapper, encrypted, or layered layouts, investigate the actual filesystem device rather than choosing a member disk.

## Optional read-only mountability probe

Only for the selected drive and an authorized diagnostic request, if `ntfs-3g.probe` is already available:

```sh
device='/dev/disk/by-id/REPLACE-WITH-VERIFIED-PARTITION'
sudo ntfs-3g.probe --readonly "$device"
probe_status=$?
printf 'ntfs-3g.probe exit status: %s\n' "$probe_status"
```

Use administrator access only if needed, with normal user authentication. Preserve the result rather than hiding a failure with `|| true`. Upstream defines 0 as mountable, 12 as invalid NTFS, 13 as inconsistency/hardware/driver failure, 14 as hibernation, 15 as unclean unmount, 16 as already in use, and 19 as insufficient privilege. See the [upstream probe manual](https://github.com/tuxera/ntfs-3g/blob/edge/src/ntfs-3g.probe.8.in) for the full list.

**Read-only mountability is not write safety.** A successful probe can coexist with dirty flags or conditions that prevent writes. Do not use `--readwrite` as a read-only diagnostic substitute, or treat `ntfsfix -n` as equivalent to Mount Medic's enforced read-only probe. If the evidence is incomplete, report it as incomplete.

## Manual limited repair with ntfsfix

Offer this only when the user explicitly wants a manual Linux repair and understands its limits. `ntfsfix` repairs a limited set of NTFS structures, resets the journal, and requests a Windows consistency check. It is not `chkdsk` or a data-recovery guarantee. The standalone route lacks Mount Medic's complete checks, exclusive block-device claim, and durable attempt tracking; the inspection helper does not supply those guarantees.

Before a write:

1. Establish the exact target and the user's authorization for this command on this device. Back up readable important files to a different drive first. For I/O errors, repeated disconnects, or suspected hardware failure, stop repair and prioritize imaging/recovery.
2. Rule out Windows hibernation/Fast Startup or cached Windows state. A successful read-only probe does not do this. If uncertain, use the [Windows procedure](windows-repair.md) to resume the original installation and shut it down fully; do not delete hibernation files.
3. Verify the target is NTFS, unmounted, and unused. Close its applications, stop watchers/automounters, and investigate any holders or other namespaces. Ask separately before a normal unmount; do not force it, automatically unmount, or repair a mounted filesystem. Incomplete access or visibility means stop.
4. Recheck current identity and review existing diagnostic errors. Unknown conditions, unsupported storage layouts, an interrupted operation still running, or a prior safety refusal must be resolved before trying another tool.

After those conditions and the informed per-drive authorization are satisfied, run **once**, with the placeholder replaced by the freshly verified partition:

```sh
device='/dev/disk/by-id/REPLACE-WITH-VERIFIED-PARTITION'
sudo ntfsfix "$device"
repair_status=$?
printf 'ntfsfix exit status: %s\n' "$repair_status"
```

Prefer ordinary `ntfsfix`, which retains/sets the request for Windows to check the volume. Do not silently add `-d` to clear the dirty flag and Windows-check request, `-b` to clear bad-sector records, or force/hibernation-removal options. Preserve stdout, stderr, and the exit status; allow completion without a repair-killing timeout. Do not retry automatically after failure or interruption. See the [upstream ntfsfix manual](https://github.com/tuxera/ntfs-3g/blob/edge/ntfsprogs/ntfsfix.8.in).

Success means the utility completed its limited work. Arrange the requested Windows check, review important files, and seek separate authorization for normal mounting. If Linux still refuses the mount, retain the evidence and use the Windows guide rather than repeatedly clearing flags or switching tools.
