# Repair with Windows

[Back to the skill](../SKILL.md)

Use this guide when NTFS damage needs Windows repair, such as an MFT mirror mismatch, or when Linux repair has not resolved the problem. Windows `chkdsk` can check and repair more NTFS structures, but it cannot guarantee that every file is recovered. Mount Medic does not need to be installed. This is a manual procedure; the skill does not automatically create VMs, attach disks, or run Windows commands.

## Before attempting repair

- Back up important files to another drive if they are readable. Repair changes filesystem metadata and is not a substitute for recovering irreplaceable data.
- **If the drive reports I/O errors, repeatedly disconnects, or appears to be failing, stop here.** Preserve it with an appropriate image or clone before attempting filesystem repair; work on a copy. Consider professional recovery for irreplaceable data. The [GNU ddrescue manual](https://www.gnu.org/software/ddrescue/manual/ddrescue_manual.html) explains recovery from failing media.
- Let any running disk operation finish and stop tools watching the target. If Mount Medic is running, quit its watcher using the application's Quit action; closing its window alone is not enough. Keep other disk tools away from the target during Windows repair.
- Identify the physical drive and partition by size, model/serial, and partition layout. Labels and drive letters alone are not reliable identifiers. Never accept a Windows prompt to initialize or format the problem drive.

## Option 1: Boot Windows directly

This is the simpler route for a Windows system partition, a drive containing the running Linux installation, or a hibernated Windows session.

1. Save your Linux work and boot the Windows installation that uses the drive.
2. If the problem is hibernation or cached Windows metadata, resume the original Windows session, save your work, and perform a full shutdown. Disable Fast Startup if it keeps leaving the volume unavailable to Linux. This condition alone does not call for `chkdsk`; return to Linux and check again after shutdown. See the [NTFS-3G guidance on Windows hibernation and fast restarting](https://github.com/tuxera/ntfs-3g/blob/edge/src/ntfs-3g.8.in).
3. If filesystem damage still needs repair, follow [Run the Windows check](#run-the-windows-check) below.

## Option 2: Use a Windows VM (advanced)

A Windows VM can repair a **separate data drive** without rebooting Linux. Use an existing Windows VM with its own system disk, and attach the target as an additional disk. Do not boot your physical Windows installation inside the VM for this procedure, or pass through a disk containing the running Linux system.

The guest needs access to the actual block device. A shared folder or network drive is insufficient. Choose the attachment method supported by your hypervisor:

- **External USB drive:** unmount every partition on that drive in Linux, then attach the USB device to the guest. For VirtualBox, see [USB settings](https://docs.oracle.com/en/virtualization/virtualbox/7.2/user/working-with-vms.html).
- **Internal data drive:** use your hypervisor's documented physical disk or partition passthrough. For VirtualBox, see [raw disk access](https://docs.oracle.com/en/virtualization/virtualbox/7.2/user/AdvancedTopics.html). Double-check the selected device before granting access; choosing the wrong disk can destroy unrelated data.

Before starting the guest, ensure the target is unmounted and unused on Linux. For whole-disk passthrough, this applies to every partition on that disk. Prevent the desktop from automatically remounting it, and leave any disk watchers stopped. Only the guest may use the target until the guest has fully shut down and released it.

**Passthrough writes affect the real drive. A snapshot of the VM's system disk is not a backup of that drive.** A new Windows guest also cannot safely resolve another Windows installation's hibernated session; use Option 1 for that case.

## Run the Windows check

1. In Windows Disk Management, verify the target disk, partition, size, and assigned drive letter. If encrypted, unlock it with its legitimate recovery key or credentials first. If it appears as RAW or unallocated, or its identity is unclear, stop and investigate; do not format it.
2. Close applications and Explorer windows using the target. Open **Command Prompt → Run as administrator**.
3. Replace `X:` below with the verified Windows drive letter. It is a placeholder, not Mount Medic's `VOLUME_ID`.

```bat
REM Report filesystem status without requesting repairs
chkdsk X:

REM Repair logical filesystem errors; requires exclusive access
chkdsk X: /f
```

Run the commands separately and review the first report before authorizing repair. If Windows cannot lock the volume, close its users; for a system volume, accept a scheduled boot-time check when appropriate. Avoid forcing a dismount with `/x`. Do not routinely add `/r`: it adds a bad-sector scan and is not the first step for failing hardware. See [Microsoft's command reference](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/chkdsk) for options and limitations.

4. Let the check finish and read its final report. Keep power stable; do not suspend the VM, detach the disk, or shut down during repair. If errors remain or I/O failures appear, stop repeated repair attempts and prioritize data preservation.
5. Verify important files, save the report, and fully shut down Windows. For a VM, release the passed-through device before allowing Linux to access it again.
6. Back in Linux, use the [standalone inspection steps](linux-recovery.md#collect-evidence) to identify the drive again and review Windows' repair report before an explicitly requested normal mount. If Mount Medic is installed, you can instead run `mount-medic list`, verify its current ID, and run `mount-medic check VOLUME_ID --json` for a read-only check. A changed app identity remains unmanaged; review it before granting permissions again.

Mount Medic's Linux VM test results do not validate this Windows procedure. Follow your hypervisor's documentation for attachment details and Windows' own repair results for the target drive.
