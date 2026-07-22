import os
import platform
import unittest
from unittest.mock import patch

from app.core.verifier import VerificationEngine
from app.core.link_manager import LinkManager

class TestDryRunAutocorrect(unittest.TestCase):
    def setUp(self):
        self.base_dir = "/tmp/test_dry_run"
        os.makedirs(self.base_dir, exist_ok=True)
        self.engine = VerificationEngine()
        
    def tearDown(self):
        import shutil
        if os.path.exists(self.base_dir):
            shutil.rmtree(self.base_dir)
            
    @patch('app.core.verifier.platform.system', return_value="Windows")
    def test_windows_long_path_truncation(self, mock_system):
        long_dir = "A" * 200
        long_file = "B" * 60 + ".txt"
        
        plan_nested = {
            long_dir: {
                long_file: {
                    "__type__": "file",
                    "target_filename": long_file
                }
            }
        }
        
        src_path = os.path.join(self.base_dir, long_file)
        with open(src_path, "w") as f:
            f.write("test")
            
        errors = self.engine.verify_plan(self.base_dir, plan_nested)
        
        self.assertEqual(errors, {})
        
        target_filename = plan_nested[long_dir][long_file]["target_filename"]
        self.assertTrue(target_filename.endswith(".txt"))
        self.assertTrue(len(target_filename) < len(long_file), f"Expected length to decrease, but it was {len(target_filename)} vs {len(long_file)}")
        
        dest_path = os.path.join(self.base_dir, long_dir, target_filename)
        self.assertTrue(len(dest_path) + 4 <= 260, f"Path is still too long: {len(dest_path) + 4}")
        
    @patch('app.core.verifier.platform.system', return_value="Linux")
    def test_unix_filename_truncation(self, mock_system):
        long_file = "C" * 250 + ".txt"
        plan = {
            long_file: {
                "__type__": "file",
                "target_filename": long_file
            }
        }
        
        src_path = os.path.join(self.base_dir, long_file)
        with open(src_path, "w") as f:
            f.write("test")
            
        errors = self.engine.verify_plan(self.base_dir, plan)
        self.assertEqual(errors, {})
        
        target_filename = plan[long_file]["target_filename"]
        self.assertTrue(target_filename.endswith(".txt"))
        self.assertTrue(len(target_filename) < len(long_file))
        self.assertTrue(len(target_filename) + 4 <= 255)

    @patch('app.core.verifier.platform.system', return_value="Linux")
    @patch('app.core.verifier.LinkManager.get_link_info')
    def test_symlink_shadow_suffix(self, mock_get_link_info, mock_system):
        mock_get_link_info.return_value = {"type": "symlink"}
        
        # Max is 255. Shadow is 44 + 4 collision = 48 extra.
        # Target length 207 + 48 = 255 (safe).
        # Target length 208 + 48 = 256 (unsafe, should truncate).
        
        long_file = "C" * 208 + ".txt"
        plan = {
            long_file: {
                "__type__": "file",
                "target_filename": long_file
            }
        }
        
        src_path = os.path.join(self.base_dir, long_file)
        with open(src_path, "w") as f:
            f.write("test")
            
        errors = self.engine.verify_plan(self.base_dir, plan)
        self.assertEqual(errors, {})
        
        target_filename = plan[long_file]["target_filename"]
        self.assertTrue(target_filename.endswith(".txt"))
        self.assertEqual(len(target_filename), 207)
        self.assertEqual(len(target_filename) + 48, 255)

    @patch('app.core.verifier.platform.system', return_value="Linux")
    def test_unix_absolute_path_limit(self, mock_system):
        # 4096 limit. We can create a deep dict to simulate a long path.
        # The deep path will exceed 4096. 
        long_dir = "D" * 200
        filename = "file.txt"
        
        src_path = os.path.join(self.base_dir, filename)
        with open(src_path, "w") as f:
            f.write("test")
            
        # The path length will be approx: len(base_dir) + 20 * 201 + 8 = 4050. Let's make it 21.
        plan = {}
        curr = plan
        for i in range(21):
            curr[long_dir] = {}
            curr = curr[long_dir]
        curr[filename] = {"__type__": "file", "target_filename": filename}
        
        errors = self.engine.verify_plan(self.base_dir, plan)
        # Should be an error because truncating filename 'file.txt' might not be enough to fix a huge directory tree length!
        self.assertIn(filename, errors)
        self.assertIn("Path exceeds 4096 characters", errors[filename])

    @patch('app.core.verifier.platform.system', return_value="Windows")
    def test_untruncatable_file(self, mock_system):
        # Only extension is provided and it's too long
        long_dir = "E" * 250
        long_file = ".ext" * 10
        
        plan = {
            long_dir: {
                long_file: {
                    "__type__": "file",
                    "target_filename": long_file
                }
            }
        }
        
        src_path = os.path.join(self.base_dir, long_file)
        with open(src_path, "w") as f:
            f.write("test")
            
        errors = self.engine.verify_plan(self.base_dir, plan)
        self.assertIn(long_file, errors)
        self.assertEqual(errors[long_file], "Path exceeds 260 characters")

if __name__ == "__main__":
    unittest.main()
