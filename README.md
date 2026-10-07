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

Run this command in a terminal, **as your normal user**:

```sh
curl -fsSL https://raw.githubusercontent.com/aakashH242/mount-medic/main/install.sh | bash -s -- --download
```

You'll need `curl`, `bash`, and Python 3.11+. On Debian/Ubuntu, Fedora, Arch, openSUSE, and Alpine, the installer offers to install any other packages it needs. Run it without `sudo`; it asks for administrator access when needed.

Alternatively, from a checkout:

```sh
./install.sh
```

Review the summary, enter your administrator password when asked, and choose your notification, security, and startup preferences. Notification sounds are on by default, and admin approval lasts **1 hour**. You can change these later in Settings. **On Arch, installing missing packages includes a full system upgrade.**

Nothing is enrolled, inspected, repaired, or mounted during installation. Once installed, open **Mount Medic** from your application menu.

<details>
<summary>Dependencies, manual setup, and installation details</summary>

The download command installs the latest stable release. The guided installer shows what it will install and asks for administrator access once. Your notification and startup choices are saved afterward.

For manual setup or distributions without a built-in package command, the requirements are Linux, Python 3.11+, a C compiler, `make`, `pkg-config`, the distribution's `libntfs-3g` development package, NTFS utilities including `ntfsfix`, and util-linux. Desktop operation additionally needs GTK 3/PyGObject, a system and session D-Bus, UDisks2, polkit, and the desktop's authentication agent. Ayatana AppIndicator or AppIndicator is optional. The installer supplies package commands for the distro families listed above; it does not configure a missing desktop session or authentication agent.

`pip install` only installs the Python CLI; use the guided installer for the native probe and privileged integration. Run `mount-medic doctor --json` to inspect dependencies and service availability.

Installed code lives in `/usr/local/lib/mount-medic`; the launcher is `/usr/local/bin/mount-medic`. Integration consists of a system D-Bus activation file, its access policy, and two application-specific polkit actions. The root worker exits after 30 seconds idle once remembered approvals end. There is no permanent root daemon or general passwordless command grant.

Without D-Bus/polkit, local discovery remains available. An administrator can explicitly run the installed CLI with `sudo` for manual operations. Unattended repair needs the installed worker and an active local desktop session. Headless GUI requests fail with a CLI fallback message.

On Alpine/OpenRC, desktop authorization requires `polkit-elogind`, a running `elogind` and `eudev`, and a desktop authentication agent such as `polkit-gnome`. The dependency command includes the session-aware polkit package; the app does not enable system services or change the desktop's authentication policy.

</details>

## Use

Open **Mount Medic** from your application menu and select a drive. Enable monitoring if you want background checks; automatic repair and mounting are separate choices and are off by default.

Enter your administrator password once when changing drive permissions or requesting a repair. Approval lasts for **1 hour**, or until you quit Mount Medic. Choose **1–24 hours** during setup or in **Settings → Security**. Closing the window keeps approval while the tray app runs. Mount Medic never saves your password; installing updates and some system mount actions can ask separately.

### Command line

Prefer a terminal? These commands cover the usual tasks. Use `mount-medic --help` or `mount-medic COMMAND --help` for more options.

#### Check drives

```sh
mount-medic gui                     # Open the desktop window
mount-medic doctor --json           # Check dependencies and service availability
mount-medic list                    # List discovered drives and their volume IDs
mount-medic check VOLUME_ID --json  # Check one drive read-only without enrolling it
mount-medic check --json            # Check enrolled drives only
```

Replace `VOLUME_ID` with the current ID from `list`. It identifies the drive using its filesystem and hardware identity, not just its label or device path. A changed identity becomes unmanaged and needs fresh permission.

#### Monitoring and automatic actions

```sh
mount-medic configure VOLUME_ID                             # Show this drive's saved permissions
mount-medic configure VOLUME_ID --monitor on                # Enable background read-only checks
mount-medic configure VOLUME_ID --monitor on --auto-repair on # Also authorize eligible automatic repairs
mount-medic configure VOLUME_ID --auto-mount on             # Allow mounting after a successful repair
mount-medic watch                                          # Start the session watcher
```

Checks, automatic repair, and automatic mounting are separate permissions. Changing write permissions requires confirmation and administrator authentication. Only enable automatic actions for drives you intend to manage.

Each CLI command starts a new process, so it can ask for your password again. For several manual commands in the same terminal, use `sudo mount-medic ...`; sudo remembers your approval for its usual short period.

#### Repair and mounting

```sh
mount-medic repair VOLUME_ID --dry-run --json # Preview a proposed repair
mount-medic repair VOLUME_ID                 # Request a repair, clearing the dirty flag
mount-medic repair VOLUME_ID --keep-dirty    # Repair while retaining a Windows-check request
mount-medic mount VOLUME_ID                  # Request a normal UDisks mount
mount-medic unmount VOLUME_ID                # Request a normal unmount; stop if busy
```

A repair preview performs discovery only; it does not certify repair eligibility or change the drive. At execution, the app checks safety again and asks for confirmation and administrator authentication.

The default repair clears the dirty flag and its Windows-check request. `--keep-dirty` retains or sets that request. Repair and mounting have separate results: repair can succeed while mounting fails.

After a failed or interrupted repair, review its outcome before using `mount-medic repair VOLUME_ID --retry`. An already-started privileged repair can continue after its client exits; establish that it has finished before retrying.

#### Update the app

```sh
mount-medic update check                   # Check for a new version
mount-medic update install                 # Install the available update
mount-medic update ignore 1.2.0            # Skip notifications for this version
mount-medic update install --dry-run --json # Preview an update without installing
```

Ignoring a version does not stop you installing it later. Updates ask for administrator access and keep your saved settings.

Add `--json` for output you can use in scripts. Checks return **0** when no attention is needed, **1** for a drive finding or available update, and **2** when the command cannot complete.

## Use with an agent

The [Mount Medic skill](skill/SKILL.md) works **without installing the app or downloading its source code**. It includes read-only inspection, native Linux recovery guidance, and manual Windows/VM instructions. If the app is installed, the agent can use its guarded CLI.

Install directly from GitHub with the Skills CLI (requires Node.js/npm):

```sh
npx skills add aakashH242/mount-medic --skill mount-medic
```

Alternatively, install the complete [`skill/` folder](skill) with your agent's skill installer. For Codex, ask:

```text
Use $skill-installer to install https://github.com/aakashH242/mount-medic/tree/main/skill as mount-medic.
```

Then try: **“Use $mount-medic to diagnose why my NTFS drive will not mount. Do not install anything or repair the drive.”** Restart Codex if the skill does not appear; see the [official skill documentation](https://learn.chatgpt.com/docs/build-skills) for setup.

The helper uses Python 3.8+ and existing Linux utilities; native commands are also documented. Adding the skill does not install the app or grant repair permission. Manual repair needs explicit per-drive authorization and lacks the app's full safety checks.

## Startup and notifications

Choose login startup and message durations in **Settings**. In-app confirmations close after 3 seconds by default; desktop notifications use 10 seconds. You can change both during installation or under **Settings → Notifications**. New drives stay unmanaged until you enable monitoring; **Ignore This Drive** suppresses discovery notifications until you remove the entry from **Ignore List**. Ignoring a drive never grants repair or mounting permission.

Notification sounds are on by default. Turn off **Play notification sounds** during setup or in **Settings → Notifications** for quiet alerts. Sounds follow your desktop settings, including Do Not Disturb.

**Check now** in the tray reports its result in a notification without opening the window. In the app, checks and saved changes show a brief confirmation, with warnings and errors marked clearly.

**Closing the window keeps the app running in the background.** Reopen it from the launcher or tray; use **Settings → Quit Mount Medic** or the tray's **Quit** to stop it. Quitting does not change login startup or saved drive permissions. Background actions never request administrator authentication.

## Recovery limits

Mount Medic repairs only eligible **dirty-flag or unclean-journal** problems on NTFS drives. Mounted or busy devices, unsupported layouts, hibernation, cached Windows state, I/O errors, MFT mirror mismatches, and incomplete or uncertain checks stop repair.

For hibernation, resume the original Windows installation, save your work, and fully shut it down; disable Fast Startup if needed. For I/O errors or suspected hardware failure, prioritize imaging/recovery rather than repeated repair attempts. A successful repair does not guarantee every file is intact.

**Need Windows repair? Follow the [Repair with Windows guide](skill/references/windows-repair.md).** It covers booting Windows directly, running `chkdsk`, and checking the drive again in Linux. It also explains using a Windows VM with exclusive access to a separate data drive. VM setup and disk attachment are manual; Mount Medic does not manage VMs.

## Safety boundary

Write approvals belong to a specific user and drive identity. The app rechecks the target and requires exclusive access before repair. Failed or interrupted repairs require review before another attempt; do not bypass a safety refusal with another tool.

Mount Medic never formats drives, clears bad-sector records, removes hibernation files, forces mounts or unmounts, or edits `fstab`. These safeguards cannot prevent hardware failures or interference from unrelated privileged disk tools.

## State and removal

Before reinstalling or uninstalling, let any disk operation finish and quit through **Settings → Quit Mount Medic** or the tray's **Quit**. Closing the window is not enough. Wait 30 seconds for the worker to become idle; the installer refuses to run over a live worker or disk operation.

### Updates

Mount Medic checks for updates every hour while it is running, including when the window is closed. When a new version is available, choose **Install** or **Ignore this version** from the notification. You won't get the same notification every hour; a later version can still notify you.

You can also open **Settings → Updates** to check now, read what's new, or install a version you previously ignored. If you're offline, try again when connected. Quitting the app pauses automatic checks until you start it again.

Installation asks for administrator access and restarts the app afterward. Let any drive check or repair finish first. Your drive permissions, history, preferences, and login startup choice stay as they are. If extra packages are needed, you'll see them before approving; on Arch this includes a full system upgrade.

**Using v1.0.0?** Run the installer once more to get the updater. After that, you can update from the app or [command line](#update-the-app).

<details>
<summary>Trouble updating?</summary>

If an update fails, Mount Medic restores the previous app version. If a power cut or interrupted installation leaves it asking for recovery, run:

```sh
sudo /usr/local/lib/mount-medic/installer --recover
```

Then open the app again. Recovery restores the app; it does not change your drive permissions or undo approved package installations.

If that command is missing or isn't recognized, use the manual reinstall below.

For more detail about a failed GUI update, check `~/.local/state/mount-medic/update.log` (or your `XDG_STATE_HOME`). You can also use the manual reinstall below.

</details>

### Manual reinstall

Run the same guided installer again as your normal user:

```sh
curl -fsSL https://raw.githubusercontent.com/aakashH242/mount-medic/main/install.sh | bash -s -- --download
```

From an existing checkout, use `./install.sh` instead. You do not need to uninstall first. Reinstallation replaces application files while retaining drive permissions and diagnostic history; the installer asks about notification duration and login startup again.

### Uninstall

```sh
mount-medic uninstall
```

Approve the removal and authenticate when asked. Uninstall removes the installed app and your login startup entry, and revokes write approvals. Modified or unrelated files, preferences, and diagnostic history are preserved. Other users can remove their own inactive startup entries.

Your saved settings and history normally live in `~/.config/mount-medic/` and `~/.local/state/mount-medic/`. Remove those folders separately if you want to delete them. There is no telemetry; update checks contact GitHub, and the app downloads an update only when you choose to install it.

## License

Mount Medic's source is MIT licensed; see [LICENSE](LICENSE). It dynamically links the distribution's `libntfs-3g` and invokes separately installed NTFS utilities. Dependencies retain their own licenses and notices; no utility implementation is vendored. Review the distribution's NTFS-3G terms when redistributing binaries.
