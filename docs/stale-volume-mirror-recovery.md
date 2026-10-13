# Recovering a stale volume metadata mirror

On 13 October 2026, an NTFS data drive would not mount. Mount Medic reported **MFT mirror mismatch** and did not repair it. The mismatch was recoverable, but the app's original probe stopped before its safety checks could finish.

## What was found

The primary volume metadata record had its dirty flag set. Its mirrored copy still had the old flag. Both records were structurally valid; their meaningful difference was the dirty flag. The mirror's update-sequence token was also older. Other mirrored records agreed.

A separate read-only inspection found no hibernation or cached Windows journal state. SMART passed with no critical warning or media errors, and the selected device's kernel log showed no hardware I/O errors. Fresh identity and an exclusive device claim established that the intended drive was unmounted and unused.

## What fixed the drive

Before repair, the boot-sector region and the four primary and mirrored MFT records were saved, with checksums. The repair ran **once**, after explicit authorization, using the same limited command as Mount Medic's normal policy:

```text
ntfsfix -d /proc/self/fd/CLAIMED_DESCRIPTOR
```

The descriptor referred to the already verified and exclusively claimed partition; it was not an unchecked device name. The existing operation lock and durable attempt log were retained, and the mutating process was allowed to finish.

The output reported:

```text
Correcting differences in $MFTMirr record 3...OK
Processing of $MFT and $MFTMirr completed successfully.
```

The command exited with status 0. The original read-only probe then completed and reported **clean**. After separate approval, a normal mount succeeded and directory access was confirmed. This restored access to the drive and its repository checkout.

The metadata snapshots are a record of the affected structures, not a backup of the whole drive. This procedure does not prove that every file is intact.

## What Mount Medic now checks

For this narrowly defined stale-flag case, the probe validates the original copies and uses a temporary, in-memory mirror view to finish its normal read-only checks. It does not write during diagnosis. Only a complete dirty-flag or unclean-journal result can become eligible for the existing repair transaction.

Hibernation, cached Windows metadata, I/O failures, malformed records, other mirror differences, unsupported layouts and incomplete checks still stop repair. The new path is limited to standard NTFS 3.1 volume metadata with four mirrored records fitting one cluster. It is not general MFT reconstruction.

`ntfsfix -d` can reset the journal and clear the dirty flag and its Windows-check request. It does not replace Windows `chkdsk` or guarantee recovery of every file. A failed or interrupted repair needs review; it must not be retried automatically.
