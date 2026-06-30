import ast
import os
import config
from core.logger import logger

class ConfigurationValidator:
    def __init__(self):
        self.max_folders = config.MAX_FOLDERS
        self.max_workers = config.MAX_WORKERS
        self.min_df = config.MIN_DF
        self.max_df = config.MAX_DF
        self.log_file = config.LOG_FILE
        self.stop_words = config.STOP_WORDS

    def validate_startup(self):
        """Prevents application startup if critical configuration conflicts are detected."""
        if not isinstance(self.max_folders, int) or self.max_folders <= 0:
            raise ValueError(f"Critical Conflict: MAX_FOLDERS must be a positive integer, got {self.max_folders}")
        if not isinstance(self.max_workers, int) or self.max_workers <= 0:
            raise ValueError(f"Critical Conflict: MAX_WORKERS must be a positive integer, got {self.max_workers}")
        if not isinstance(self.min_df, int) or self.min_df < 0:
            raise ValueError(f"Critical Conflict: MIN_DF must be a non-negative integer, got {self.min_df}")
        if not (0 < self.max_df <= 1.0):
            raise ValueError(f"Critical Conflict: MAX_DF must be between 0 and 1.0, got {self.max_df}")
        if not isinstance(self.log_file, str) or not self.log_file:
            raise ValueError(f"Critical Conflict: LOG_FILE must be a valid string, got {self.log_file}")
        if not isinstance(self.stop_words, set):
            raise ValueError(f"Critical Conflict: STOP_WORDS must be a set, got {type(self.stop_words)}")
            
        # Block use of hardcoded overrides for the maximum folder setting
        self._check_for_hardcoded_overrides()
        
        logger.info("Startup validation passed. All global settings are tracked.")

    def _check_for_hardcoded_overrides(self):
        """Scans analyzer.py to ensure maximum folder limits are not shadowed by local logic."""
        analyzer_path = os.path.join(os.path.dirname(__file__), 'analyzer.py')
        if not os.path.exists(analyzer_path):
            return
            
        with open(analyzer_path, 'r', encoding='utf-8') as f:
            tree = ast.parse(f.read())
            
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id == 'min':
                    for arg in node.args:
                        if isinstance(arg, ast.Name) and arg.id == 'max_folders':
                            raise RuntimeError("Hardcoded override detected: max_folders is shadowed by local logic in analyzer.py. Move constraints to validator.")

    def validate_clustering_constraints(self, requested_folders, num_documents, num_vocab):
        """
        Validator rule that applies the limit to the number of folders based on document constraints.
        Overrides must be explicitly declared and validated.
        """
        if requested_folders > self.max_folders:
            logger.warning(f"Requested folders ({requested_folders}) exceeds global maximum ({self.max_folders}). Capping.")
            requested_folders = self.max_folders

        max_possible = min(requested_folders, num_documents // 2, num_vocab)
        actual_k = max(max_possible, 2)
        
        if actual_k < requested_folders:
            logger.info(f"Validator applied limits: Reduced folders from {requested_folders} to {actual_k} (docs={num_documents}, vocab={num_vocab})")
            
        return actual_k

validator = ConfigurationValidator()
