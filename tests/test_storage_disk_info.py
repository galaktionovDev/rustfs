import copy
import importlib.util
from pathlib import Path
import unittest


spec = importlib.util.spec_from_file_location(
    'storage_disk_info', Path(__file__).parents[1] / 'library' / 'storage_disk_info.py')
info = importlib.util.module_from_spec(spec)
spec.loader.exec_module(info)

UUID1 = '605f9d01-4f14-4b4a-a58a-c1d69e1c1223'
UUID2 = 'c8ab256b-bd58-48fb-9897-3a1309172299'


def manifest():
    return [
        {'name': 'disk-1', 'uuid': UUID1, 'serial': 'virtio-' + UUID1[:20],
         'size': 10, 'path': '/dev/vdb', 'mount': '/app/disk1', 'fstype': 'xfs'},
        {'name': 'disk-2', 'uuid': UUID2, 'serial': 'virtio-' + UUID2[:20],
         'size': 10, 'path': '/dev/vdc', 'mount': '/app/disk2', 'fstype': 'xfs'},
    ]


def device(path, identifier, children=None, kind='disk', mountpoint=None):
    return {'name': path, 'type': kind, 'size': 10 * 1024 ** 3,
            'serial': identifier, 'children': children or [], 'mountpoint': mountpoint}


def snapshot():
    root = device('/dev/dm-0', 'root-lv', kind='lvm', mountpoint='/')
    boot = device('/dev/vda2', 'boot-part', [root], kind='part')
    rows = [device('/dev/vda', 'system-disk', [boot]),
            device('/dev/vdb', UUID1[:20]), device('/dev/vdc', UUID2[:20])]
    devices = info.flatten_devices(rows)
    for item in devices.values():
        item['identifiers'] = [item['serial']]
    mounts = [{'source': '/dev/dm-0', 'target': '/', 'fstype': 'ext4'}]
    signatures = {'/dev/vdb': {}, '/dev/vdc': {}}
    return devices, mounts, signatures


class DiskInfoTests(unittest.TestCase):
    def setUp(self):
        self.disks = manifest()
        self.devices, self.mounts, self.signatures = snapshot()

    def plan(self, require_mounted=False):
        return info.validate_state(self.disks, self.devices, self.mounts,
                                   self.signatures, require_mounted)

    def reject(self, pattern):
        with self.assertRaisesRegex(info.DiskValidationError, pattern):
            self.plan()

    def test_equal_sizes_are_resolved_by_identifiers(self):
        self.assertEqual([disk['path'] for disk in self.plan()], ['/dev/vdb', '/dev/vdc'])

    def test_enumeration_order_does_not_change_assignment(self):
        self.devices = dict(reversed(list(self.devices.items())))
        self.assertEqual([disk['uuid'] for disk in self.plan()], [UUID1, UUID2])

    def test_changed_device_path_is_resolved_again(self):
        self.devices['/dev/nvme1n1'] = self.devices.pop('/dev/vdb')
        self.signatures['/dev/nvme1n1'] = self.signatures.pop('/dev/vdb')
        self.assertEqual(self.plan()[0]['path'], '/dev/nvme1n1')

    def test_system_disk_with_arbitrary_name_is_rejected(self):
        self.devices['/dev/xvda'] = self.devices.pop('/dev/vda')
        self.devices['/dev/vda2']['parents'] = {'/dev/xvda'}
        self.disks = [dict(self.disks[0], serial='system-disk', uuid='system-disk')]
        self.reject('system disk')

    def test_missing_identity_does_not_fall_back_to_size_or_path(self):
        self.disks[0]['serial'] = 'missing-identity'
        self.reject('Serial not found')

    def test_uuid_only_manifest(self):
        self.disks[0].pop('serial')
        self.assertEqual(self.plan()[0]['path'], '/dev/vdb')

    def test_conflicting_serial_and_uuid(self):
        self.disks[0]['uuid'] = UUID2
        self.reject('conflicting')

    def test_ambiguous_identity_is_rejected(self):
        self.devices['/dev/vdd'] = copy.deepcopy(self.devices['/dev/vdb'])
        self.reject('ambiguous')

    def test_duplicate_physical_device_is_rejected(self):
        self.disks[1].update(uuid=UUID1, serial=self.disks[0]['serial'])
        self.reject('same device')

    def test_duplicate_mount_is_rejected(self):
        self.disks[1]['mount'] = self.disks[0]['mount']
        self.reject('Duplicate')

    def test_partition_table_without_visible_children_is_rejected(self):
        self.signatures['/dev/vdc'] = {'PTTYPE': 'gpt'}
        self.reject('Partition table')

    def test_child_device_is_rejected(self):
        self.devices['/dev/vdb']['children'].add('/dev/vdb1')
        self.reject('dependent devices')

    def test_foreign_filesystem_is_rejected(self):
        self.signatures['/dev/vdc'] = {'TYPE': 'ext4', 'UUID': 'foreign-fs'}
        self.reject('Unexpected signature')

    def test_filesystem_signature_without_type_is_rejected(self):
        self.signatures['/dev/vdc'] = {'UUID': 'unknown-fs'}
        self.reject('Unexpected signature')

    def test_valid_existing_xfs_is_preserved(self):
        self.signatures['/dev/vdb'] = {'TYPE': 'xfs', 'UUID': 'existing-xfs'}
        result = self.plan()[0]
        self.assertEqual(result['fs_uuid'], 'existing-xfs')
        self.assertEqual(result['fs_type'], 'xfs')

    def test_recorded_filesystem_uuid_is_checked(self):
        self.signatures['/dev/vdb'] = {'TYPE': 'xfs', 'UUID': 'different-xfs'}
        self.disks[0]['fs_uuid'] = 'expected-xfs'
        self.reject('recorded XFS UUID')

    def test_mount_target_and_device_must_be_the_same_pair(self):
        self.mounts.extend([
            {'source': '/dev/vdb', 'target': '/app/other', 'fstype': 'xfs'},
            {'source': '/dev/vdc', 'target': '/app/disk1', 'fstype': 'xfs'}])
        self.reject('mount target conflict')

    def test_directory_on_root_is_not_a_valid_mount(self):
        self.mounts.append({'source': '/dev/dm-0', 'target': '/app/disk1', 'fstype': 'ext4'})
        self.reject('mount target conflict')

    def test_all_existing_mounts_can_be_verified(self):
        for disk in self.disks:
            self.signatures[disk['path']] = {'TYPE': 'xfs', 'UUID': disk['name'] + '-fs'}
            self.mounts.append({'source': disk['path'], 'target': disk['mount'], 'fstype': 'xfs'})
        self.assertTrue(all(disk['mounted'] for disk in self.plan(True)))

    def test_final_report_requires_actual_mount(self):
        with self.assertRaisesRegex(info.DiskValidationError, 'not mounted'):
            self.plan(True)

    def test_short_serial_is_not_matched_by_prefix(self):
        self.assertFalse(info.identifier_matches('disk-1', 'disk-12'))

    def test_mount_path_cannot_escape_app_directory(self):
        self.disks[0]['mount'] = '/app/../etc'
        self.reject('Invalid data mount')

    def test_read_only_device_is_rejected(self):
        self.devices['/dev/vdb']['ro'] = True
        self.reject('Read-only device')

    def test_undersized_device_is_rejected(self):
        self.devices['/dev/vdb']['size'] = 9 * 1024 ** 3
        self.reject('smaller')

    def test_unknown_system_disk_is_rejected(self):
        self.mounts = []
        self.devices['/dev/dm-0']['mountpoint'] = None
        self.reject('system disk ancestry')

    def test_duplicate_filesystem_uuids_are_rejected(self):
        for disk in self.disks:
            self.signatures[disk['path']] = {'TYPE': 'xfs', 'UUID': 'duplicate-fs'}
        self.reject('Duplicate filesystem UUID')

    def test_read_only_filesystem_is_rejected(self):
        self.signatures['/dev/vdb'] = {'TYPE': 'xfs', 'UUID': 'existing-xfs'}
        self.mounts.append({'source': '/dev/vdb', 'target': '/app/disk1',
                            'fstype': 'xfs', 'options': 'ro,noatime'})
        self.reject('filesystem is mounted read-only')


if __name__ == '__main__':
    unittest.main()
