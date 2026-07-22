import os
import platform
import unittest
from unittest.mock import patch

from app.core.verifier import VerificationEngine

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

if __name__ == "__main__":
    unittest.main()
