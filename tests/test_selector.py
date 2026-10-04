import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gpu_selector as selector


class FakeApp:
    def __init__(self, path):
        self.path = path

    def get_id(self):
        return self.path.name

    def get_filename(self):
        return str(self.path)


class SelectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config_patch = patch.object(selector, 'CONFIG', self.root / 'config')
        self.apps_patch = patch.object(selector, 'APPS', self.root / 'applications')
        self.config_patch.start()
        self.apps_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.addCleanup(self.apps_patch.stop)
        selector.APPS.mkdir()
        self.gpu = selector.GPU('GPU-test', 'Test GPU', '0000:01:00.0',
                                '10de', '1234', 'nvidia', '/unused', 'GPU-test')

    def desktop(self, directory):
        directory.mkdir(exist_ok=True)
        path = directory / 'example.desktop'
        path.write_text('[Desktop Entry]\nType=Application\nName=Example\nExec=/usr/bin/true %U\nDBusActivatable=true\n')
        return path

    def test_cuda_uses_stable_uuid_and_cleans_inherited_selection(self):
        with patch.dict(os.environ, {'DRI_PRIME': 'wrong', 'CUDA_VISIBLE_DEVICES': '0'}):
            env = selector.child_environment(self.gpu, 'cuda')
        self.assertEqual(env['CUDA_VISIBLE_DEVICES'], 'GPU-test')
        self.assertNotIn('DRI_PRIME', env)
        self.assertEqual(env['OLLAMA_VULKAN'], '0')

    def test_non_cuda_gpu_uses_cpu_for_cuda(self):
        self.gpu.uuid = ''
        self.assertEqual(selector.gpu_environment(self.gpu, 'cuda')['CUDA_VISIBLE_DEVICES'], '-1')

    def test_restore_removes_generated_entry_and_preserves_source(self):
        source = self.desktop(self.root / 'system')
        original = source.read_bytes()
        selector.save_preference(FakeApp(source), self.gpu, 'cuda')
        override = selector.APPS / source.name
        self.assertIn('X-GPU-Selector=true', override.read_text())
        self.assertIn('DBusActivatable=false', override.read_text())
        selector.restore_preference(source.name)
        self.assertFalse(override.exists())
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(selector.settings(), {})

    def test_existing_user_entry_is_restored_byte_for_byte(self):
        source = self.desktop(selector.APPS)
        original = source.read_bytes()
        selector.save_preference(FakeApp(source), self.gpu, 'cuda')
        selector.save_preference(FakeApp(source), self.gpu, 'cuda')
        selector.restore_preference(source.name)
        self.assertEqual(source.read_bytes(), original)

    def test_restore_refuses_to_overwrite_external_changes(self):
        source = self.desktop(self.root / 'system')
        selector.save_preference(FakeApp(source), self.gpu, 'cuda')
        override = selector.APPS / source.name
        override.write_text('externally edited')
        with self.assertRaises(ValueError):
            selector.restore_preference(source.name)
        self.assertEqual(override.read_text(), 'externally edited')

    def test_unsupported_nvidia_opengl_does_not_write_preferences(self):
        source = self.desktop(self.root / 'system')
        with self.assertRaises(ValueError):
            selector.save_preference(FakeApp(source), self.gpu, 'opengl')
        self.assertEqual(selector.settings(), {})
        self.assertFalse((selector.APPS / source.name).exists())


if __name__ == '__main__':
    unittest.main()
