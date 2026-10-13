'Pure predicate regressions, source only; no application or kernel launch.'
import copy
import importlib.util
from pathlib import Path
import unittest

path = Path(__file__).with_name('startup_owned_fd.py')
spec = importlib.util.spec_from_file_location('owned', path)
owned = importlib.util.module_from_spec(spec)
spec.loader.exec_module(owned)


class FrontierTests(unittest.TestCase):
    def setUp(self):
        self.standard = {key: {'stdio': int(key)} for key in ('0', '1', '2')}
        self.snapshot = {key: {'identity': value} for key, value in self.standard.items()}
        self.snapshot['7'] = {'identity': dict(owned.IDENTITY), 'target': owned.TARGET, 'stable': True}

    def deny(self, snapshot):
        with self.assertRaises(RuntimeError):
            owned.certify_snapshot(snapshot, self.standard)

    def test_exact_resource_not_fixed_number(self):
        self.assertEqual(owned.certify_snapshot(self.snapshot, self.standard), {'7': owned.IDENTITY})

    def test_unknown_target(self):
        self.snapshot['7']['target'] = '/dev/null'
        self.deny(self.snapshot)

    def test_each_metadata_field(self):
        for key in owned.IDENTITY:
            candidate = copy.deepcopy(self.snapshot)
            candidate['7']['identity'][key] += 1
            self.deny(candidate)

    def test_writable(self):
        self.snapshot['7']['identity']['open_flags'] |= 2
        self.deny(self.snapshot)

    def test_replaced(self):
        self.snapshot['7']['stable'] = False
        self.deny(self.snapshot)

    def test_extra_descriptor(self):
        self.snapshot['8'] = copy.deepcopy(self.snapshot['7'])
        self.deny(self.snapshot)

    def test_missing_resource(self):
        del self.snapshot['7']
        self.deny(self.snapshot)

    def test_stdio_changed(self):
        self.snapshot['0']['identity'] = {}
        self.deny(self.snapshot)


class PreparationProjectionTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {str(fd): {'identity': {'parent_stdio': fd}}
                         for fd in (0, 1, 2)}
        self.snapshot['9'] = {'identity': dict(owned.IDENTITY),
                              'target': owned.TARGET, 'stable': True}
        self.retained = {str(fd): {'identity': {'owned_handle': fd,
                                               'descriptor_flags': 1},
                                  'target': '/private/retained-' + str(fd),
                                  'stable': True} for fd in (4, 5, 6)}
        self.snapshot.update(copy.deepcopy(self.retained))

    def test_parent_slot_not_child_slot(self):
        self.assertEqual(owned.project_prepared_frontier(self.snapshot, self.retained),
                         {'3': owned.IDENTITY})

    def test_parent_controller_only_handle_not_projected(self):
        self.snapshot['8'] = copy.deepcopy(self.retained['4'])
        self.assertEqual(owned.project_prepared_frontier(self.snapshot, self.retained),
                         {'3': owned.IDENTITY})

    def test_duplicate_runtime_target_denies(self):
        self.snapshot['8'] = copy.deepcopy(self.snapshot['9'])
        with self.assertRaises(RuntimeError):
            owned.project_prepared_frontier(self.snapshot, self.retained)

    def test_declared_handle_swap_denies(self):
        self.snapshot['4']['identity']['owned_handle'] = 99
        with self.assertRaises(RuntimeError):
            owned.project_prepared_frontier(self.snapshot, self.retained)

    def test_declared_handle_target_changed_denies(self):
        self.snapshot['5']['target'] = '/unowned'
        with self.assertRaises(RuntimeError):
            owned.project_prepared_frontier(self.snapshot, self.retained)

    def test_missing_or_duplicate_handle_shape_denies(self):
        del self.retained['6']
        with self.assertRaises(RuntimeError):
            owned.project_prepared_frontier(self.snapshot, self.retained)


if __name__ == '__main__':
    unittest.main()
