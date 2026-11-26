#!/usr/bin/env bash
# Installs or removes the Apple SuperDrive helper for Linux:
#   - superdrive.py, the wake/eject/status helper, in /usr/local/lib/apple-superdrive-helper
#   - a udev rule (and a systemd unit on systemd systems) that wakes the drive on every plug-in
#     and keeps USB autosuspend off for it
#   - a polkit policy that lets the desktop user wake the drive from the window without a password
#   - the Qt window, an apple-superdrive-helper launcher and a desktop entry
#
# Usage: sudo bash install.sh [--venv] [--no-gui] [--uninstall]
#   --venv       download PySide6 from PyPI into a private virtual environment if it is not installed
#   --no-gui     install only the automatic wake and the command-line helper
#   --uninstall  remove everything this script installed
set -euo pipefail

prefix=/usr/local
libdir=$prefix/lib/apple-superdrive-helper
helper=$libdir/superdrive.py
launcher=$prefix/bin/apple-superdrive-helper
desktop_file=$prefix/share/applications/apple-superdrive-helper.desktop
rule_file=/etc/udev/rules.d/99-apple-superdrive.rules
unit_file=/etc/systemd/system/apple-superdrive-wake@.service
policy_dir=/usr/share/polkit-1/actions
policy_file=$policy_dir/local.apple-superdrive-helper.policy
system_python=/usr/bin/python3
source_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

usage() {
    sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

use_venv=false
install_gui=true
action=install
for argument in "$@"; do
    case $argument in
        --venv) use_venv=true ;;
        --no-gui) install_gui=false ;;
        --uninstall) action=uninstall ;;
        -h | --help) usage; exit 0 ;;
        *) echo "Unknown option: $argument" >&2; usage >&2; exit 2 ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    echo "This installs system files. Run it with sudo: sudo bash $0 $*" >&2
    exit 1
fi

has_systemd() {
    [[ -d /run/systemd/system ]] && command -v systemctl >/dev/null
}

uninstall() {
    rm -f "$rule_file" "$unit_file" "$policy_file" "$launcher" "$desktop_file"
    rm -rf "$libdir" /run/apple-superdrive-helper
    udevadm control --reload-rules
    if has_systemd; then
        systemctl daemon-reload
    fi
    echo "Removed the Apple SuperDrive helper."
}

install_helper() {
    if [[ ! -x $system_python ]]; then
        echo "Python 3 is required at $system_python. Install it from your distribution first." >&2
        exit 1
    fi
    install -d -m 0755 "$libdir"
    install -m 0755 "$source_dir/superdrive.py" "$helper"
}

# 05ac:1500 is the USB ID of the Apple USB SuperDrive (check yours with lsusb). The wake
# rule matches its optical block device; the helper checks the SCSI inquiry strings again.
install_auto_wake() {
    local keep_powered='ACTION=="add", SUBSYSTEM=="usb", ATTR{idVendor}=="05ac", ATTR{idProduct}=="1500", ATTR{power/control}="on"'
    local match='ACTION=="add", SUBSYSTEM=="block", KERNEL=="sr[0-9]*", ATTRS{idVendor}=="05ac", ATTRS{idProduct}=="1500"'
    local wake_action
    if has_systemd; then
        # udev kills long-running RUN programs and sandboxes them, so it starts a unit instead.
        cat >"$unit_file" <<EOF
[Unit]
Description=Wake the Apple USB SuperDrive at /dev/%I
BindsTo=dev-%i.device
After=dev-%i.device

[Service]
Type=oneshot
ExecStart=$helper wake --device /dev/%I --source udev
EOF
        chmod 0644 "$unit_file"
        systemctl daemon-reload
        wake_action='TAG+="systemd", ENV{SYSTEMD_WANTS}+="apple-superdrive-wake@%k.service"'
    else
        wake_action="RUN+=\"$helper wake --device /dev/%k --source udev\""
    fi
    cat >"$rule_file" <<EOF
# Apple USB SuperDrive (05ac:1500)
# Keeps USB autosuspend off for the drive, which can otherwise drop it off the bus.
$keep_powered
# Wakes the drive each time it is plugged in, so it accepts discs.
$match, $wake_action
EOF
    chmod 0644 "$rule_file"
    udevadm control --reload-rules
}

install_polkit_policy() {
    if [[ ! -d $policy_dir ]]; then
        echo "polkit is not installed, so the window will ask you to wake the drive with sudo instead."
        return
    fi
    cat >"$policy_file" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE policyconfig PUBLIC "-//freedesktop//DTD PolicyKit Policy Configuration 1.0//EN"
  "http://www.freedesktop.org/standards/PolicyKit/1/policyconfig.dtd">
<policyconfig>
  <action id="local.apple-superdrive-helper.wake">
    <description>Wake an Apple USB SuperDrive</description>
    <message>Waking the SuperDrive sends it a vendor command, which needs administrator rights.</message>
    <defaults>
      <allow_any>auth_admin</allow_any>
      <allow_inactive>auth_admin</allow_inactive>
      <allow_active>yes</allow_active>
    </defaults>
    <annotate key="org.freedesktop.policykit.exec.path">$helper</annotate>
  </action>
</policyconfig>
EOF
    chmod 0644 "$policy_file"
}

install_window() {
    install -m 0644 "$source_dir/superdrive_gui.py" "$libdir/superdrive_gui.py"

    local python=$system_python
    if ! "$python" -c 'import PySide6.QtWidgets' 2>/dev/null; then
        if $use_venv; then
            echo "Downloading PySide6 into $libdir/venv ..."
            "$system_python" -m venv "$libdir/venv"
            "$libdir/venv/bin/pip" install --disable-pip-version-check --quiet PySide6-Essentials
            python=$libdir/venv/bin/python
        else
            echo
            echo "PySide6 is not installed, so the window will not open yet. Either:"
            echo "  - install it from your distribution (for example python3-pyside6.qtwidgets on"
            echo "    Debian and recent Ubuntu, python3-pyside6 on Fedora, pyside6 on Arch), or"
            echo "  - run this script again with --venv to download PySide6 from PyPI."
            echo
        fi
    fi

    install -d -m 0755 "$(dirname "$launcher")" "$(dirname "$desktop_file")"
    cat >"$launcher" <<EOF
#!/bin/sh
exec "$python" "$libdir/superdrive_gui.py" "\$@"
EOF
    chmod 0755 "$launcher"
    cat >"$desktop_file" <<EOF
[Desktop Entry]
Type=Application
Name=SuperDrive Helper
Comment=Wake, eject and check an Apple USB SuperDrive
Exec=$launcher
Icon=drive-optical
Terminal=false
Categories=System;Utility;
EOF
    chmod 0644 "$desktop_file"
    if command -v update-desktop-database >/dev/null; then
        update-desktop-database -q "$(dirname "$desktop_file")" || true
    fi
}

if [[ $action == uninstall ]]; then
    uninstall
    exit 0
fi

install_helper
install_auto_wake
install_polkit_policy
if $install_gui; then
    install_window
fi

echo "Installed. The SuperDrive is now woken automatically whenever it is plugged in."
echo "Waking any SuperDrive that is already connected:"
"$helper" wake --source cli || echo "  (Nothing was woken now; plug the drive in and it will be.)"
if $install_gui; then
    echo "Open the window from your applications menu (SuperDrive Helper) or run: apple-superdrive-helper"
fi
