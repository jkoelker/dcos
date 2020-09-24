#!/opt/mesosphere/bin/python

"""
The script allows to add network interfaces and ip addresses multiple times
ip command returns 2 as exit code if interface or ipaddr already exists [1]
dcos-net-setup.py checks output of ip command and returns success exit code [2]

[1] ExecStartPre=-/usr/bin/ip link add name type dummy
[2] ExecStartPre=/path/dcos-net-setup.py ip link add name type dummy

Also the script prevents from duplicating iptables rules [3]

[3] ExecStartPre=/path/dcos-net-setup.py iptables --wait -A FORWARD -j ACCEPT

The script allows to add configuration for networkd
"""

import datetime
import filecmp
import logging
import os
import platform
import shutil
import subprocess
import sys


def run(cmd, *args, **kwargs):
    command = ' '.join(cmd)
    print('command: `{}`'.format(command))
    result = subprocess.run(cmd, *args, **kwargs)
    if result.stderr:
        sys.stderr.buffer.write(result.stderr)
    print('command: `{}` exited with status `{}`'.format(
        command,
        result.returncode,
    ))
    return result


def main(argv):
    if argv[1:4] in [['ip', 'link', 'add'], ['ip', 'addr', 'add'], ['ip', '-6', 'addr']]:
        result = run(argv[1:], stderr=subprocess.PIPE)
        if result.stderr.strip().endswith(b'File exists'):
            return 0

        return result.returncode

    elif argv[1] == 'iptables':
        # check whether a rule matching the specification does exist
        argv = ['-C' if arg in ['-A', '-I'] else arg for arg in argv[1:]]

        result = run(argv)
        if result.returncode != 0:
            # if it doesn't exist append or insert that rules
            result = run(argv[1:])

        return result.returncode

    elif argv[1] == '--ipv6':
        if os.getenv('DCOS_NET_IPV6', 'true') == 'false':
            return 0
        else:
            del argv[1]
            return main(argv)

    elif argv[1:3] == ['networkd', 'add'] and len(argv) == 4:
        return add_networkd_config_for_coreos(argv[3])

    return run(argv[1:]).returncode


def add_networkd_config_for_coreos(src: str) -> int:
    # systemd-networkd, when enabled, will wipe the configurations like IP
    # address of network interfaces and this behavior happens only on coreos
    # This problem is tracked by:
    # https://jira.mesosphere.com/browse/DCOS_OSS-1790
    # We need to mark interfaces managed by DC/OS as unmanaged when networkd is
    # enabled on coreos
    if platform.system() != "Linux" or "coreos" not in platform.release():
        return 0

    networkd = b'systemd-networkd.service'
    networkd_path = '/etc/systemd/network'

    # Check if there is networkd
    result = run(
        ['systemctl', 'list-unit-files', networkd],
        stdout=subprocess.PIPE,
    )
    if result.returncode != 0:
        return result.returncode
    if networkd not in result.stdout:
        return result.returncode

    # Copy the configuration
    bname = os.path.basename(src)
    dst = os.path.join(networkd_path, bname)

    # Ensure the destination directory exists
    os.makedirs(networkd_path, mode=0o755, exist_ok=True)
    if not safe_filecmp(src, dst):
        shutil.copyfile(src, dst)

    # Restart networkd only if it's active
    result = run(
        ['systemctl', 'is-active', networkd],
        stdout=subprocess.PIPE,
    )
    if result.returncode != 0:
        result.returncode = 0
        return result.returncode

    # Restart networkd only if the configuration is updated
    mtime = os.path.getmtime(dst)
    result = run(
        ['systemctl', 'show', '--value', '--property', 'ActiveEnterTimestamp',
         networkd],
        stdout=subprocess.PIPE,
    )
    if result.returncode == 0:
        active_enter_timestamp = result.stdout.strip().decode()
        try:
            started = datetime.datetime.strptime(
                active_enter_timestamp,
                '%a %Y-%m-%d %H:%M:%S %Z')
            if started.timestamp() > mtime:
                return result.returncode
        except ValueError:
            logging.warning('Unexpected ActiveEnterTimestamp value: "%s"',
                            active_enter_timestamp)

    # Restart networkd
    return run(['systemctl', 'restart', networkd]).returncode


def safe_filecmp(src, dst):
    try:
        return filecmp.cmp(src, dst)
    except FileNotFoundError:
        return False


if __name__ == "__main__":
    sys.exit(main(sys.argv))
