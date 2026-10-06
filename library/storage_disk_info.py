#!/usr/bin/python
"""Read-only validation of explicitly assigned whole data disks."""

import json
import math
import os
import posixpath
import re


DOCUMENTATION = r'''
---
module: storage_disk_info
short_description: Validate assigned data disks without modifying them
description:
  - Resolves disks by their reported serial or platform UUID.
  - Rejects system disks, partitions, conflicting mounts and foreign signatures.
options:
  disks:
    description: Per-host disk manifest returned by provisioning.
    required: true
    type: list
    elements: dict
  require_mounted:
    description: Require every disk to be mounted at its declared target.
    type: bool
    default: false
author: Storage automation maintainers
'''


class DiskValidationError(ValueError):
    pass


def normalize_identifier(value):
    value = str(value or '').lower()
    value = re.sub(r'^(virtio-|scsi-|wwn-|nvme-|ata-)', '', value)
    return re.sub(r'[^a-z0-9]', '', value)


def identifier_matches(expected, observed):
    expected = normalize_identifier(expected)
    observed = normalize_identifier(observed)
    if not expected or not observed:
        return False
    if expected == observed:
        return True
    # Some virtual disk serials contain a truncated UUID. Short identifiers
    # must match exactly; ambiguity is rejected by the resolver below.
    return min(len(expected), len(observed)) >= 16 and (
        expected.startswith(observed) or observed.startswith(expected))


def flatten_devices(rows, canonical=lambda path: path):
    devices = {}

    def visit(row, parent=None):
        path = canonical(row['name'])
        device = devices.setdefault(path, {'parents': set(), 'children': set()})
        device.update({key: value for key, value in row.items() if key != 'children'})
        if parent:
            device['parents'].add(parent)
            devices[parent]['children'].add(path)
        for child in row.get('children') or []:
            visit(child, path)

    for row in rows:
        visit(row)
    return devices


def validate_manifest(disks):
    if not isinstance(disks, list) or not disks:
        raise DiskValidationError('The per-host disk manifest is empty')
    names, targets = set(), set()
    for disk in disks:
        if not isinstance(disk, dict):
            raise DiskValidationError('Each manifest entry must be a mapping')
        for field in ('name', 'size', 'uuid', 'path', 'mount', 'fstype'):
            if not disk.get(field):
                raise DiskValidationError('Missing disk field: ' + field)
        if any(not isinstance(disk[field], str) for field in ('name', 'uuid', 'path', 'mount', 'fstype')):
            raise DiskValidationError('Disk identifiers and paths must be strings')
        if disk['fstype'] != 'xfs':
            raise DiskValidationError('Only whole-disk XFS is supported')
        if isinstance(disk['size'], bool) or not math.isfinite(float(disk['size'])) or float(disk['size']) <= 0:
            raise DiskValidationError('Disk size must be positive')
        target = disk['mount']
        if not isinstance(target, str) or not target.startswith('/app/') or (
                posixpath.normpath(target) != target or '\n' in target or '\x00' in target):
            raise DiskValidationError('Invalid data mount: ' + str(target))
        if disk['name'] in names or target in targets:
            raise DiskValidationError('Duplicate disk name or mount target')
        names.add(disk['name'])
        targets.add(target)


def resolve_disks(disks, devices):
    validate_manifest(disks)
    resolved, claimed = [], set()
    for disk in disks:
        candidates = {
            path: device for path, device in devices.items()
            if device['type'] == 'disk'}
        matches = {}
        for field in ('serial', 'uuid'):
            if disk.get(field):
                matches[field] = {
                    path for path, device in candidates.items()
                    if any(identifier_matches(disk[field], value)
                           for value in device.get('identifiers', []))}
        serial_matches = matches.get('serial', set())
        uuid_matches = matches.get('uuid', set())
        if disk.get('serial') and not serial_matches:
            raise DiskValidationError('Serial not found for ' + disk['name'])
        selected = serial_matches if disk.get('serial') else uuid_matches
        if serial_matches and uuid_matches:
            selected = serial_matches & uuid_matches
        if len(selected) != 1:
            raise DiskValidationError('Missing, conflicting or ambiguous identity for ' + disk['name'])
        path = next(iter(selected))
        if path in claimed:
            raise DiskValidationError('Two manifest entries identify the same device')
        claimed.add(path)
        resolved.append(dict(disk, path=path))
    return resolved


def validate_state(disks, devices, mounts, signatures, require_mounted=False):
    resolved = resolve_disks(disks, devices)
    protected = set()

    def protect(path):
        if path not in devices or path in protected:
            return
        protected.add(path)
        for parent in devices[path]['parents']:
            protect(parent)

    for mount in mounts:
        if mount['target'] in ('/', '/boot', '/boot/efi'):
            protect(mount['source'])
    for path, device in devices.items():
        if device.get('mountpoint') in ('/', '/boot', '/boot/efi'):
            protect(path)
    if not protected:
        raise DiskValidationError('Cannot identify the system disk ancestry')

    reports, filesystem_uuids = [], set()
    for disk in resolved:
        path, target = disk['path'], disk['mount']
        device = devices[path]
        if path in protected:
            raise DiskValidationError('A system disk cannot be used for data: ' + path)
        if device['children']:
            raise DiskValidationError('Partitions or dependent devices exist on ' + path)
        if device.get('ro') not in (None, False, 0, '0'):
            raise DiskValidationError('Read-only device: ' + path)
        expected_bytes = float(disk['size']) * 1024 ** 3
        if int(device['size']) < expected_bytes:
            raise DiskValidationError('Device is smaller than its declared size: ' + path)
        probe = signatures[path]
        if probe.get('PTTYPE') or any(key.startswith('PART_ENTRY_') for key in probe):
            raise DiskValidationError('Partition table found on ' + path)
        fs_type, fs_uuid = probe.get('TYPE', ''), probe.get('UUID', '')
        if fs_type not in ('', 'xfs') or (probe and not fs_type):
            raise DiskValidationError('Unexpected signature on ' + path)
        if fs_type == 'xfs' and not fs_uuid:
            raise DiskValidationError('XFS UUID is missing on ' + path)
        if fs_uuid and fs_uuid in filesystem_uuids:
            raise DiskValidationError('Duplicate filesystem UUID on assigned disks')
        if fs_uuid:
            filesystem_uuids.add(fs_uuid)
        if disk.get('fs_uuid') and disk['fs_uuid'] != fs_uuid:
            raise DiskValidationError('The recorded XFS UUID does not match ' + path)
        by_device = [mount for mount in mounts if mount['source'] == path]
        by_target = [mount for mount in mounts if mount['target'] == target]
        if any(mount['source'] != path for mount in by_target) or any(
                mount['target'] != target or mount.get('subtree') for mount in by_device):
            raise DiskValidationError('Device and mount target conflict for ' + disk['name'])
        mounted = bool(by_device and by_target)
        if mounted and (fs_type != 'xfs' or any(
                mount['fstype'] != 'xfs' for mount in by_target)):
            raise DiskValidationError('The mounted filesystem is not XFS')
        if mounted and any('ro' in mount.get('options', '').split(',') for mount in by_target):
            raise DiskValidationError('The data filesystem is mounted read-only')
        if require_mounted and not mounted:
            raise DiskValidationError('Data disk is not mounted: ' + target)
        reports.append(dict(disk, fs_type=fs_type, fs_uuid=fs_uuid, mounted=mounted))
    return reports


def main():
    from ansible.module_utils.basic import AnsibleModule

    module = AnsibleModule(argument_spec={
        'disks': {'type': 'list', 'elements': 'dict', 'required': True},
        'require_mounted': {'type': 'bool', 'default': False},
    }, supports_check_mode=True)
    try:
        validate_manifest(module.params['disks'])
        lsblk = module.get_bin_path('lsblk', required=True)
        findmnt = module.get_bin_path('findmnt', required=True)
        blkid = module.get_bin_path('blkid', required=True)
        _, output, _ = module.run_command([
            lsblk, '--json', '--bytes', '--paths', '--output',
            'NAME,TYPE,SIZE,SERIAL,WWN,MOUNTPOINT,RO'], check_rc=True)
        devices = flatten_devices(json.loads(output)['blockdevices'], os.path.realpath)
        for device in devices.values():
            device['identifiers'] = [device.get('serial'), device.get('wwn')]
        if os.path.isdir('/dev/disk/by-id'):
            for identifier in os.listdir('/dev/disk/by-id'):
                path = os.path.realpath('/dev/disk/by-id/' + identifier)
                if path in devices:
                    devices[path]['identifiers'].append(identifier)
        _, output, _ = module.run_command([
            findmnt, '--evaluate', '--json', '--output', 'SOURCE,TARGET,FSTYPE,OPTIONS'], check_rc=True)
        mounts = []

        def visit_mount(row):
            source = row.get('source') or ''
            mounts.append({'source': os.path.realpath(source.split('[')[0]),
                           'target': row['target'], 'fstype': row.get('fstype'),
                           'options': row.get('options') or '',
                           'subtree': '[' in source})
            for child in row.get('children') or []:
                visit_mount(child)

        for row in json.loads(output)['filesystems']:
            visit_mount(row)
        signatures = {}
        for disk in resolve_disks(module.params['disks'], devices):
            rc, output, error = module.run_command([blkid, '-p', '-o', 'export', disk['path']])
            if rc not in (0, 2) or (rc == 2 and (output.strip() or error.strip())):
                raise DiskValidationError('Cannot inspect signatures on ' + disk['path'])
            signatures[disk['path']] = dict(
                line.split('=', 1) for line in output.splitlines() if '=' in line)
        reports = validate_state(module.params['disks'], devices, mounts, signatures,
                                 module.params['require_mounted'])
        for disk in reports:
            target = disk['mount']
            if os.path.realpath(target) != target:
                raise DiskValidationError('A symlink is present in the mount path: ' + target)
            if os.path.lexists(target) and not os.path.isdir(target):
                raise DiskValidationError('The mount target is not a directory: ' + target)
            if not disk['mounted'] and os.path.isdir(target) and os.listdir(target):
                raise DiskValidationError('The unmounted target is not empty: ' + target)
        module.exit_json(changed=False, disks=reports)
    except (DiskValidationError, ValueError, KeyError, TypeError, OSError) as error:
        module.fail_json(msg=str(error))


if __name__ == '__main__':
    main()
