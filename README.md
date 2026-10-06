<img src="mount_medic/assets/io.github.aakashH242.MountMedic.png" width="64" height="64" alt="Mount Medic app icon">

# Mount Medic

**Drive care, on your terms.**

Mount Medic is a small Linux desktop app and command-line tool for people who share NTFS drives between Windows and Linux. It helps diagnose drives that will not mount and offers **limited repairs, with your permission**.

A power cut, crash, or unsafe disconnect can leave an NTFS drive marked as dirty or with unfinished filesystem changes. Linux may then refuse to mount it. Windows hibernation and Fast Startup can also prevent safe write access, even when the drive is not corrupted. Getting access back can mean interrupting your work, booting into Windows to shut it down properly or run `chkdsk`, and then returning to Linux.

Mount Medic was written to reduce that back-and-forth for the limited cases Linux tools can address. It checks the drives you choose and can repair a dirty flag or an unclean journal when its safety checks pass. Problems that need Windows, including hibernation and more serious damage, remain outside its repair scope.

Python handles discovery and policy, GTK 3 provides the native window, and a small C probe uses the distribution's `libntfs-3g` for read-only diagnosis.

**New drives are unmanaged.** Discovery does not enroll a drive or run Mount Medic's filesystem probe. Enable checks for each drive, then separately authorize automatic repair and, optionally, automatic mounting.

> `ntfsfix` is not Windows `chkdsk`. It repairs limited NTFS metadata problems and resets the journal. The automatic policy uses `ntfsfix -d`, which also clears the dirty flag and its request for a subsequent Windows check. Success does not certify every file or full filesystem integrity. Back up valuable data before authorizing writes. See the [upstream manual](https://github.com/tuxera/ntfs-3g/blob/edge/ntfsprogs/ntfsfix.8.in).

## Install

**The guided installer can install the required packages for you** on Debian/Ubuntu, Fedora, Arch, openSUSE, and Alpine. It shows the package-manager command and asks for your permission before running it.

Run this one command in a terminal, **as your normal user**:

```sh
curl -fsSL https://raw.githubusercontent.com/aakashH242/mount-medic/main/install.sh | bash -s -- --download
```

To start this command, you need `curl`, `bash`, `tar`, and Python 3.11+. The installer handles the remaining packages after you approve. Run it without `sudo`; it requests administrator access when needed.

Alternatively, from a checkout:

```sh
./install.sh
```

Follow the prompts to install dependencies, review the application files, set the notification duration (default **10 seconds**), and choose whether Mount Medic starts after login. **On Arch, installing dependencies includes a full system upgrade.**

Nothing is enrolled, inspected, repaired, or mounted during installation. Once installed, open **Mount Medic** from your application menu.

<details>
<summary>Dependencies, manual setup, and installation details</summary>

The download command fetches the current `main` source into a temporary directory, runs the guided installer, and cleans up afterward. The installer builds the native probe without root, previews the installed files and privileged integration, and requests administrator authentication for system changes.

For manual setup or distributions without a built-in package command, the requirements are Linux, Python 3.11+, a C compiler, `make`, `pkg-config`, the distribution's `libntfs-3g` development package, NTFS utilities including `ntfsfix`, and util-linux. Desktop operation additionally needs GTK 3/PyGObject, a system and session D-Bus, UDisks2, polkit, and the desktop's authentication agent. Ayatana AppIndicator or AppIndicator is optional. The installer supplies package commands for the distro families listed above; it does not configure a missing desktop session or authentication agent.

`pip install` only installs the Python CLI; use the guided installer for the native probe and privileged integration. Run `mount-medic doctor --json` to inspect dependencies and service availability.

Installed code lives in `/usr/local/lib/mount-medic`; the launcher is `/usr/local/bin/mount-medic`. Integration consists of a system D-Bus activation file, its access policy, and two application-specific polkit actions. The root worker exits after 30 seconds idle. There is no permanent root daemon or general passwordless command grant.

Without D-Bus/polkit, local discovery remains available. An administrator can explicitly run the installed CLI with `sudo` for manual operations. Unattended repair needs the installed worker and an active local desktop session. Headless GUI requests fail with a CLI fallback message.

On Alpine/OpenRC, desktop authorization requires `polkit-elogind`, a running `elogind` and `eudev`, and a desktop authentication agent such as `polkit-gnome`. The dependency command includes the session-aware polkit package; the app does not enable system services or change the desktop's authentication policy.

</details>

## Use

Open **Mount Medic** from the application menu, or run:

```sh
mount-medic gui                                             # Open the desktop window
mount-medic list                                            # List discovered volumes and their IDs
mount-medic check --json                                    # Check enrolled drives read-only; output JSON
mount-medic check VOLUME_ID --json                          # Check one drive read-only without enrolling it; output JSON
mount-medic configure VOLUME_ID --monitor on                 # Enroll this drive for automatic read-only checks
mount-medic configure VOLUME_ID --monitor on --auto-repair on # Enable checks and authorize automatic repair when safe
mount-medic configure VOLUME_ID --auto-mount on              # Allow automatic mounting after successful repair
mount-medic repair VOLUME_ID --dry-run --json                # Preview the repair plan as JSON without changing the drive
mount-medic repair VOLUME_ID                                # Request a limited repair, clearing the dirty flag on success
mount-medic repair VOLUME_ID --keep-dirty                    # Request a limited repair while retaining a Windows check request
mount-medic repair VOLUME_ID --retry                         # Explicitly retry after reviewing an unresolved repair attempt
mount-medic mount VOLUME_ID                                 # Mount the drive through UDisks, subject to safety checks
mount-medic unmount VOLUME_ID                               # Request a normal unmount; stop if the drive is busy
mount-medic watch                                          # Run the session watcher for checks, authorized actions, and notifications
```

Replace `VOLUME_ID` with the 24-character ID from `list`. It incorporates the filesystem UUID, available disk hardware identity, partition UUID, size, and partition location. Device names and labels are never sufficient authorization. A changed identity becomes unmanaged; duplicate UUIDs and weak identities cannot receive automatic repair.

`configure` without changes shows saved permissions. Checks and write permissions are separate. Granting or changing write permissions requires administrator authentication, as does every manual repair. The window offers the same controls and both manual policies. `--keep-dirty` uses ordinary `ntfsfix`, leaving or setting the request for a Windows check. Cancel at the confirmation or authentication dialog before execution. Closing the window does not terminate an already-started repair.

The native window lists discovered and previously monitored drives. Its **Monitoring** column shows **Added**, **Not added**, or **Ignored**. Select a drive and choose **Monitor** to enable background checks and choose automatic repair and mounting separately; both automatic options start off. **Permissions** edits those choices. Saving the three permissions together requires administrator authentication. **Remove** stops monitoring and revokes both automatic permissions, including for disconnected drives; diagnostic history is retained.

The window follows your GTK light/dark theme and system text size. **Drive details** expands the full identity and last check; **More** contains mounting, safe unmounting, **Ignore This Drive**, and raw diagnostics. The ink-and-teal drive mark identifies the launcher, window, tray, and notifications. Its editable SVG and PNG fallback live in `mount_medic/assets`; the fallback keeps the app icon visible without an SVG decoder.

`--dry-run` shows the proposed action and discovery information without contacting the worker, requesting authentication, enrolling, mounting, or repairing. Fresh safety checks are still mandatory at execution. Discovery and diagnostic results support `--json`. Exit statuses are **0** for success/no attention, **1** for a finding requiring attention, and **2** for unavailable or failed execution, including cancellation.

Automatic mounting is off by default and applies after successful repair. It uses normal UDisks options and verifies directory accessibility as the calling user. Mount failures remain visible even when repair succeeded. **Unmount and inspect** asks for confirmation, performs a normal unmount, and stops if busy. There is no automatic unmount.

UDisks' own permissions still apply. A background mount that would require authentication fails without prompting, even when Mount Medic's per-drive mount toggle is enabled.

## Use with an agent

The [Mount Medic skill](skill/SKILL.md) works **without installing the app or downloading its source code**. The self-contained `skill/` folder includes an inspection script, native Linux commands for explicitly authorized manual repair, and Windows/VM guidance. If the app is already installed, the agent can use its guarded CLI instead.

Install the repository's [`skill/` directory](skill) using your agent's skill installer. For Codex, ask:

```text
Use $skill-installer to install https://github.com/aakashH242/mount-medic/tree/main/skill as mount-medic.
```

Keep that folder's `SKILL.md`, `LICENSE`, `scripts/`, `references/`, and `agents/` together. It has no references to files outside the folder. For local development, you can link just `$PWD/skill` into your agent's skill directory; a normal skill installation can be an independent copy.

Invoke it with a request such as **“Use $mount-medic to diagnose why my NTFS drive will not mount. Do not install anything or repair the drive.”** If it does not appear, restart Codex. See the [official skill documentation](https://learn.chatgpt.com/docs/build-skills) for discovery and invocation details.

The standalone helper uses Python 3.8+ and existing Linux utilities; native command examples are included if Python is unavailable. Missing tools are reported, not installed silently. Manual repair needs explicit authorization for the selected drive and does not inherit the app's full safety checks. Adding the skill does not install the application, enroll drives, or grant repair permission.

## Startup and notifications

Enable or disable startup through **Settings → Startup**. One per-user XDG autostart entry starts the watcher; there is no second systemd startup mechanism. `watch` and `gui` share a session-bus single-instance application.

The open GUI also listens for drive connections. Closing a GUI-only session exits the app; a watcher started at login or with `watch` continues in the background when its window is closed.

After login, the watcher waits 15 seconds, then checks enrolled drives. UDisks events are debounced for two seconds; there is no periodic disk polling. A new unmanaged drive gets a native **NTFS drive discovered** notification with **Monitor** and **Ignore This Drive**. Monitor opens the same permissions dialog as the drive list. Closing the notification with the desktop's dismiss control leaves the drive unmonitored, without repeating the alert during that connection. Reconnecting it or starting a new watcher session can offer it again. Unchanged problems are not repeatedly announced.

**Ignore This Drive** persistently suppresses notifications for that volume identity across reconnects and app restarts. Open **Ignore List** to review ignored drives, including disconnected ones, and remove an entry to allow notifications again. Choosing Monitor and successfully saving permissions also removes its ignore. Ignores never grant repair or mounting permissions. As with enrollment, a changed filesystem or hardware identity is treated as a new drive; labels and `/dev` paths are not identifiers.

Change notification duration through **Settings → Notifications**, from 1 to 600 seconds. The app requests that timeout from the desktop and recalls the notification when it expires. Desktop policies may hide it sooner, suppress notifications, or omit action buttons; the same actions remain available in the window. Preferences and ignores are stored in `$XDG_CONFIG_HOME/mount-medic/preferences.json` and `ignored.json` (normally `~/.config/mount-medic/`). Permissions stay in the root-owned `/var/lib/mount-medic/<UID>.json`; diagnostic history stays under `$XDG_STATE_HOME/mount-medic/` (normally `~/.local/state/mount-medic/`).

The tray uses Ayatana AppIndicator or AppIndicator. Without tray support, use the launcher and notifications; no shell extensions are installed. Without UDisks event support, login and manual checks remain available with a diagnostic message. Background operations never request authentication.

## Recovery limits

This version permits repair only for a **dirty flag or an unclean journal**, after all required read-only checks complete. It supports ordinary NTFS partitions and whole-device volumes with strong identity. Mounted or busy devices, unsupported storage layouts, hibernation, cached Windows metadata, additional maintenance flags, I/O errors, incomplete checks, and unknown conditions stop repair.

MFT mirror mismatches are diagnosed but **not repaired**: the library cannot complete independent hibernation and journal exclusions on that path. Follow [Repair with Windows](skill/references/windows-repair.md) for the manual `chkdsk` procedure. A generic filesystem error is never evidence that repair is safe.

The probe overrides library device operations to reject writable opens, writes, and non-read-only ioctls. It does not use `ntfs-3g.probe --readwrite` or rely on `ntfsfix -n` for the read-only boundary. Tests include a dirty image for which both command-line checks return success.

For hibernation/cached metadata, resume Windows, save work, and fully shut it down; disable Fast Startup if needed. For unsupported damage, use [Windows `chkdsk`](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/chkdsk). For I/O failures, prioritize backup/imaging and hardware investigation. Mount Medic never guesses a Windows drive letter.

**Need Windows repair?** The [Repair with Windows guide](skill/references/windows-repair.md) covers booting Windows directly, using a Windows VM with exclusive access to a separate data drive, running `chkdsk`, and checking the drive again in Linux. VM repair is an advanced manual option, not an automatic Mount Medic feature or a guarantee of full data recovery.

## Safety boundary

- The system bus establishes the caller's UID. Root-owned approvals are scoped to that user and volume identity. User configuration cannot grant writes. Background polkit requests use flags `0`, prohibiting prompts.
- Requests accept fixed operations, validated volume IDs, and booleans. They cannot supply device paths, commands, arbitrary flags, or state locations. Executables come from trusted paths, with controlled environments and bounded output.
- One operation lock serializes diagnosis and mutation. Fresh discovery, disk sequence numbers, block identity, mount namespaces, FUSE sources, holders, and open device descriptors are checked. Incomplete visibility fails closed.
- An exclusive kernel block-device claim is retained through diagnosis, repair, and verification. Tools receive the inherited descriptor instead of an old device name. Attempts are fsynced before `ntfsfix` starts; success is recorded only after independent diagnosis and final identity verification.
- Failed, interrupted, or overdue repairs block automatic retries. A mutating child is never killed by a timeout. An overdue process retains the lock and claim until it exits, then requires an explicitly reviewed retry.
- The app never formats, clears bad-sector lists, removes hibernation files, forces mounting/unmounting, or modifies `fstab`. Applicable `fstab`/UDisks force or hibernation-removal options block app-initiated mounts. [NTFS3 documentation](https://docs.kernel.org/filesystems/ntfs3.html) discourages forcing dirty mounts.

These safeguards cannot stop unrelated privileged raw-sector writers, validate every file, prevent hardware failures, or make failing media safe. UDisks mounting occurs after the block claim is released; identity is checked again, but mounting is not an atomic transaction with diagnosis.

## State and removal

Ordinary state is under `$XDG_STATE_HOME/mount-medic` (default `~/.local/state/mount-medic`): bounded history, last checks, and notification deduplication. Startup is under `$XDG_CONFIG_HOME/autostart`. Privileged approvals and attempts are separate, root-owned records under `/var/lib/mount-medic`. There is no telemetry or application background network access.

```sh
mount-medic uninstall
```

Close the watcher and allow the worker to become idle first. Reinstall/removal refuses to run over a live worker or disk operation. Uninstall checks its manifest and hashes, preserves modified/unrelated files, revokes write approvals, and removes the invoking user's startup entry. History and preferences remain; remove retained history explicitly if unwanted. Other users can remove their own inactive startup entries.

## License

Mount Medic's source is MIT licensed; see [LICENSE](LICENSE). It dynamically links the distribution's `libntfs-3g` and invokes separately installed NTFS utilities. Dependencies retain their own licenses and notices; no utility implementation is vendored. Review the distribution's NTFS-3G terms when redistributing binaries.
