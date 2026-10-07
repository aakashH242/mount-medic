# Changelog

## 1.3.0 — 7 October 2026

Mount Medic 1.3.0 makes drive information easier to read and keeps your last check visible.

- See used space, total space and the percentage free in the drive list, Drive details and Drive diagnostics. A small usage bar appears in details and diagnostics. Unmounted drives show their capacity with “Usage unavailable.”
- Keep the last check time when adding or removing monitored drives, changing permissions, refreshing the list, or restarting after an update.
- Read Drive details in a neatly arranged panel. Select and copy individual values, including device names and drive IDs.
- See **Next steps** first in diagnostics, with clearer headings and readable values that wrap in smaller windows.
- Fixed automatic recovery showing old drive information after successfully repairing and mounting a drive. If a drive disconnects during a check, its name and identity are kept and old usage figures are cleared.

To update, open **Settings → Updates** or run `mount-medic update install`. Your saved drive permissions, preferences and check history are preserved.

## 1.2.1 — 7 October 2026

Mount Medic 1.2.1 makes app updates easier to follow.

- See a small update window while downloading, waiting for your administrator password, installing, and restarting.
- Mount Medic reopens when the update finishes and shows the result inside the app. If it was running in the tray, it returns to the tray.
- If an update fails, see whether your current version was kept. An interrupted installation still shows how to recover it.
- Get a clear message if the update installed but the app could not reopen, or if the result could not be saved.
- Keep update messages readable with large fonts, and prevent two updates from running at the same time.

To update, open **Settings → Updates** or run `mount-medic update install`. Your saved drive permissions and preferences are preserved. The new progress window is available for updates started from 1.2.1 onward; updating from an older version uses that version's update screen.

## 1.2.0 — 7 October 2026

Mount Medic 1.2.0 makes everyday drive care easier, with fewer password prompts and clearer feedback.

### Easier to use

- Enter your administrator password once for drive permission changes and repairs. Approval lasts **1 hour** by default; choose **1–24 hours** during setup or in **Settings → Security**. Quitting Mount Medic clears approval. Closing the window keeps it while the app runs in the tray. Your password is never saved. Updates and some system mount actions can still ask separately.
- Choose whether notifications play the system's default sound during setup or in **Settings → Notifications**. Sound is on by default and follows your desktop's sound and Do Not Disturb settings.
- Set how long app messages and desktop notifications stay visible. App messages default to **3 seconds** and desktop notifications to **10 seconds**.
- See progress, results, and saved changes more clearly. Checking from the tray reports the result without opening the window, and update installation reports success or failure.
- See each drive's automatic repair and automatic mounting settings directly in the drive list.
- Use a simple on/off switch for starting Mount Medic after login.

### Fixes

- Fixed the tray icon appearing dark or inactive after an icon change. It now follows the desktop's light or dark appearance.
- Aligned the app name with its icon and made switches easier to see across desktop themes.
- Fixed monitoring triggering an SELinux warning when checking anonymous kernel descriptors, including `io_uring`. Checks for real devices remain in place.

### Updating

From 1.1.0, open **Settings → Updates** or run:

```sh
mount-medic update check
mount-medic update install
```

From 1.0.0, run the install command in the README again. Your saved drive permissions and preferences are preserved.

### For contributors

- Removed the GitHub Actions workflows. Releases are tested, packaged, and published manually.

## 1.1.0 — 7 October 2026

- Added update checks and installation from the desktop and command line, with hourly checks and the option to ignore a version.
- Added a guided installer with notification and startup choices.
- Improved the desktop layout, tray operation, and background monitoring.
- Added safer update installation and recovery after an interrupted update.

## 1.0.0 — 6 October 2026

- First release of Mount Medic, with a desktop app and command-line tools for NTFS diagnosis and explicitly authorized, limited repair.
