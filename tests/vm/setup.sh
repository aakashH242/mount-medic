#!/bin/sh
# Only executed from cloud-init in the disposable, guarded test guest.
set -eu
trap 'python3 /opt/mount-medic/tests/vm/debug.py || true; sync; echo MOUNT_MEDIC_VM_FAIL' EXIT
test "$(cat /sys/class/dmi/id/product_name)" = MountMedicTest
rm -f /home/medic/desktop-result.json /home/medic/desktop-phase /home/medic/dirty-ready /tmp/namespace-ready /etc/xdg/autostart/mount-medic-vm.desktop
export DEBIAN_FRONTEND=noninteractive
case "$1" in
  ubuntu)
    apt-get update
    apt-get install -y fdisk dmsetup
    apt-get install -y python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1 build-essential pkg-config ntfs-3g ntfs-3g-dev udisks2 polkitd dbus-x11 dbus-user-session gdm3 gnome-session gnome-shell qemu-guest-agent
    usermod -aG sudo medic
    mkdir -p /etc/gdm3
    printf '[daemon]\nAutomaticLoginEnable=true\nAutomaticLogin=medic\n' > /etc/gdm3/custom.conf
    glib-compile-schemas /usr/share/glib-2.0/schemas
    ;;
  fedora)
    dnf -y install util-linux device-mapper
    dnf -y install python3-gobject gtk3 libappindicator-gtk3 gcc make pkgconf-pkg-config ntfs-3g ntfsprogs ntfs-3g-devel util-linux udisks2 polkit dbus-daemon sddm plasma-desktop plasma-workspace-wayland polkit-kde mesa-dri-drivers qemu-guest-agent
    usermod -aG wheel medic
    mkdir -p /etc/sddm.conf.d
    printf '[Autologin]\nUser=medic\nSession=plasma.desktop\n' > /etc/sddm.conf.d/autologin.conf
    ;;
  alpine)
    apk add device-mapper
    apk add python3 py3-gobject3 gtk+3.0 libayatana-appindicator build-base linux-headers pkgconf ntfs-3g ntfs-3g-progs ntfs-3g-dev util-linux udisks2 polkit-elogind polkit-gnome dbus elogind xfce4 lightdm lightdm-gtk-greeter mesa-dri-gallium xorg-server xf86-input-libinput eudev qemu-guest-agent sudo
    addgroup medic wheel
    delgroup alpine wheel || true
    printf '[Seat:*]\nautologin-user=medic\nautologin-user-timeout=0\nuser-session=xfce\n' > /etc/lightdm/lightdm.conf
    rc-service dbus start
    rc-service udev start
    udevadm trigger
    udevadm settle
    rc-service cgroups start
    rc-service elogind start
    rc-service qemu-guest-agent start
    ;;
esac
if command -v systemctl >/dev/null; then systemctl start qemu-guest-agent; fi
if command -v loginctl >/dev/null; then loginctl terminate-user medic || true; fi
cd /opt/mount-medic
make clean
make test
sleep 30
python3 install.py --apply /opt/mount-medic
python3 tests/vm/guest.py "$1"
mkdir -p /etc/xdg/autostart
printf '[Desktop Entry]\nType=Application\nName=Mount Medic VM check\nExec=/usr/bin/python3 /opt/mount-medic/tests/vm/desktop.py\n' > /etc/xdg/autostart/mount-medic-vm.desktop
case "$1" in
  ubuntu) systemctl start gdm3 ;;
  fedora) systemctl start sddm ;;
  alpine) rc-service lightdm start ;;
esac
attempts=0
last_phase=''
while test ! -f /home/medic/desktop-result.json; do
  sleep 2
  phase=$(cat /home/medic/desktop-phase 2>/dev/null || true)
  if test "$phase" != "$last_phase"; then
    echo "MOUNT_MEDIC_PHASE:$phase"
    last_phase=$phase
    if test "$phase" = DIRTY_FOR_AUTO; then
      python3 tests/vm/guest.py --dirty
      touch /home/medic/dirty-ready
    fi
  fi
  attempts=$((attempts + 1))
  test "$attempts" -lt 150
done
cat /home/medic/desktop-result.json
python3 -c 'import json; assert json.load(open("/home/medic/desktop-result.json"))["passed"]'
python3 tests/vm/guest.py --uninstall
sync
echo MOUNT_MEDIC_VM_PASS
trap - EXIT
