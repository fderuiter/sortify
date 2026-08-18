"""Unified Offline Model Loading and Resolution Utility.

Handles local-only path resolution fallback directories, offline-enforced
sandboxed loading, graceful failure exceptions, and Florence-2 visual
processing with coordinate/text sanitization.
"""

import inspect
import logging
import os
import re
import socket
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.core.shared_registry import block_external_network
from app.core.text_utils import sanitize_text

logger = logging.getLogger(__name__)

# Regular expression to match 4 consecutive location tokens in Florence-2 format
# e.g., <loc_100><loc_200><loc_300><loc_400> or with spacing
LOC_BOX_PATTERN = re.compile(
    r"<loc_(\d{1,4})>\s*<loc_(\d{1,4})>\s*<loc_(\d{1,4})>\s*<loc_(\d{1,4})>"
)


class OfflineModelLoadError(Exception):
    """Base exception for all offline model loading errors."""

    pass


class ModelWeightsNotFoundError(OfflineModelLoadError):
    """Raised when model weights cannot be found in any fallback search paths."""

    def __init__(self, model_id: str, searched_paths: List[str]):
        self.model_id = model_id
        self.searched_paths = searched_paths
        paths_str = ", ".join(f"'{p}'" for p in searched_paths)
        super().__init__(
            f"Model weights for '{model_id}' were not found in any of the searched paths: {paths_str}. "
            f"Please ensure the model bundle is downloaded and placed in one of these locations."
        )


class OfflineModelLoader:
    """Centralized loader utility to resolve, verify, and load models offline.

    Enforces local-only loading policies by running standard loader functions inside
    a sandboxed network context.
    """

    _registered_models: Dict[str, Dict[str, Any]] = {}

    @classmethod
    def register_model(
        cls, model_id: str, expected_files: Optional[List[str]] = None
    ) -> None:
        """Register a model's expected files to enable automatic resolution and integrity verification.

        Parameters
        ----------
        model_id : str
            Unique identifier of the model (e.g., 'florence-2', 'easyocr').
        expected_files : list of str, optional
            List of files that must exist in the model directory.
        """
        cls._registered_models[model_id] = {"expected_files": expected_files or []}

    @classmethod
    def resolve_model_path(cls, model_id: str) -> str:
        """Resolve the local path of a model by checking pre-defined fallback search directories.

        Parameters
        ----------
        model_id : str
            The model ID to locate.

        Returns
        -------
        str
            The absolute resolved path of the model.

        Raises
        ------
        ModelWeightsNotFoundError
            If the model cannot be resolved in any search paths.
        """
        searched_paths = []

        # Precedence 1: Environment variable custom path
        env_var_name = f"{model_id.upper().replace('-', '_')}_PATH"
        env_path = os.environ.get(env_var_name)
        if env_path:
            searched_paths.append(env_path)

        # Precedence 2: PyInstaller temporary execution directory
        if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
            meipass_path = os.path.join(sys._MEIPASS, "offline_bundle", model_id)
            searched_paths.append(meipass_path)

        # Precedence 3: Local workspace directory
        workspace_path = os.path.join(os.getcwd(), "offline_bundle", model_id)
        searched_paths.append(workspace_path)

        # Also resolve workspace path relative to path_utils base path
        try:
            from app.core.path_utils import get_base_path

            base_dir = get_base_path()
            workspace_base_path = os.path.join(base_dir, "offline_bundle", model_id)
            if workspace_base_path not in searched_paths:
                searched_paths.append(workspace_base_path)
        except Exception:
            pass

        # Precedence 4: User home directory fallback
        home_path = os.path.expanduser(f"~/.smart-autosorter/offline_bundle/{model_id}")
        searched_paths.append(home_path)

        # Deduplicate paths while preserving order
        unique_paths = []
        for p in searched_paths:
            normalized_p = os.path.abspath(p)
            if normalized_p not in unique_paths:
                unique_paths.append(normalized_p)

        # Check path existence
        for path in unique_paths:
            if os.path.exists(path) and os.path.isdir(path):
                meta = cls._registered_models.get(model_id, {})
                expected_files = meta.get("expected_files", [])

                if expected_files:
                    all_exist = True
                    for f in expected_files:
                        if not os.path.exists(os.path.join(path, f)):
                            all_exist = False
                            break
                    if all_exist:
                        return path
                else:
                    # If no specific expected files registered, ensure the directory has content
                    try:
                        if os.listdir(path):
                            return path
                    except Exception:
                        pass

        raise ModelWeightsNotFoundError(model_id, unique_paths)

    @classmethod
    def load_model(
        cls, model_id: str, loader_fn: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        """Execute the loading callable within the restricted network sandbox.

        Automatically injects `local_files_only=True` if supported.

        Parameters
        ----------
        model_id : str
            The model ID to load.
        loader_fn : Callable
            The loader function that performs model initialization.
        *args : tuple
            Positional arguments for the loader function.
        **kwargs : dict
            Keyword arguments for the loader function.

        Returns
        -------
        Any
            The initialized model or pipeline object.

        Raises
        ------
        OfflineModelLoadError
            If loading fails due to network sandboxing or errors during initialization.
        """
        # 1. Resolve path first
        try:
            model_path = cls.resolve_model_path(model_id)
        except ModelWeightsNotFoundError as e:
            logger.error(f"Failed to resolve path for offline model '{model_id}': {e}")
            raise

        # 2. Automatically inject local_files_only=True if applicable
        sig = inspect.signature(loader_fn)
        has_kwargs = any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
        if "local_files_only" in sig.parameters or has_kwargs:
            kwargs["local_files_only"] = True

        # 3. Supply model_path as first positional argument if none given
        if not args:
            args = (model_path,)

        # 4. Enforce sandboxed loading
        try:
            with block_external_network(reason=f"loading offline model {model_id}"):
                return loader_fn(*args, **kwargs)
        except (PermissionError, socket.gaierror) as e:
            logger.critical(
                f"Network request blocked during sandboxed loading of model '{model_id}': {e}"
            )
            raise OfflineModelLoadError(
                f"Model loading for '{model_id}' failed due to prohibited network access. "
                f"Verify that all model configuration and weight files are present locally in '{model_path}'."
            ) from e
        except Exception as e:
            logger.error(f"Error loading model '{model_id}' from '{model_path}': {e}")
            if not isinstance(e, OfflineModelLoadError):
                raise OfflineModelLoadError(
                    f"Failed to load offline model '{model_id}' from '{model_path}': {e}"
                ) from e
            raise


class Florence2VisualProcessor:
    """Local visual analysis processor wrapper for the Florence-2 model.

    Provides end-to-end local inference, robust coordinate extraction, and
    text sanitization.
    """

    def __init__(self, model_id: str = "florence-2"):
        self.model_id = model_id
        self.model: Any = None
        self.processor: Any = None
        self._model_path: Optional[str] = None

    def load(self) -> None:
        """Load the Florence-2 model and processor safely offline.

        Raises
        ------
        OfflineModelLoadError
            If loading weights, configuration, or processor fails.
        """
        if self.model is not None and self.processor is not None:
            return

        # Register Florence-2 standard configuration files and architecture scripts
        OfflineModelLoader.register_model(
            self.model_id,
            ["config.json", "processing_florence2.py", "modeling_florence2.py"],
        )

        try:
            self._model_path = OfflineModelLoader.resolve_model_path(self.model_id)
        except ModelWeightsNotFoundError as e:
            logger.error(f"Florence-2 resolution failed: {e}")
            raise

        # Enforce pre-execution cryptographic integrity check before code execution or model loading
        from app.core.shared_registry import SharedModelRegistry

        try:
            SharedModelRegistry.get_instance().verify_integrity(
                self.model_id, self._model_path
            )
        except Exception as e:
            logger.error(f"Florence-2 integrity check failed for '{self.model_id}': {e}")
            if not isinstance(e, OfflineModelLoadError):
                raise OfflineModelLoadError(
                    f"Florence-2 model load failed: Integrity check failed for '{self.model_id}': {e}"
                ) from e
            raise

        def load_florence_model(path: str, **kwargs: Any) -> Any:
            import torch
            from transformers import AutoModelForCausalLM

            from app.core.shared_registry import SharedModelRegistry

            # Limit torch threads to configured limit
            try:
                limit = SharedModelRegistry.get_instance().get_thread_limit()
                torch.set_num_threads(limit)
            except Exception:
                pass

            kwargs["trust_remote_code"] = True
            return AutoModelForCausalLM.from_pretrained(path, **kwargs)

        def load_florence_processor(path: str, **kwargs: Any) -> Any:
            from transformers import AutoProcessor

            kwargs["trust_remote_code"] = True
            return AutoProcessor.from_pretrained(path, **kwargs)

        try:
            self.model = OfflineModelLoader.load_model(
                self.model_id, load_florence_model
            )
            self.processor = OfflineModelLoader.load_model(
                self.model_id, load_florence_processor
            )
        except Exception as e:
            logger.error(
                f"Failed to load local Florence-2 processor/model components: {e}"
            )
            raise OfflineModelLoadError(f"Florence-2 model load failed: {e}") from e

    def process_image(
        self,
        image_path: str,
        task_prompt: str = "<OD>",
        image_size: Optional[Tuple[int, int]] = None,
    ) -> Dict[str, Any]:
        """Perform visual analysis task on local image and return parsed results.

        Parameters
        ----------
        image_path : str
            The path to the input image.
        task_prompt : str
            The prompt instructing the model (e.g., '<OD>', '<CAPTION>').
        image_size : tuple of (int, int), optional
            The original size of the image as (width, height).

        Returns
        -------
        dict
            Contains parsed, cleaned coordinates and text elements.
        """
        import torch
        from PIL import Image

        self.load()

        if not os.path.exists(image_path):
            raise FileNotFoundError(f"Visual asset file not found: {image_path}")

        image = Image.open(image_path).convert("RGB")
        if image_size is None:
            image_size = image.size

        # 1. Local sandboxed inference execution
        try:
            with block_external_network(
                reason=f"Florence-2 local inference ({task_prompt})"
            ):
                inputs = self.processor(
                    text=task_prompt, images=image, return_tensors="pt"
                )

                with torch.no_grad():
                    generated_ids = self.model.generate(
                        input_ids=inputs["input_ids"],
                        pixel_values=inputs["pixel_values"],
                        max_new_tokens=1024,
                        num_beams=3,
                        do_sample=False,
                    )

                generated_text = self.processor.batch_decode(
                    generated_ids, skip_special_tokens=False
                )[0]
        except Exception as e:
            logger.error(f"Inference processing failed for visual task: {e}")
            raise OfflineModelLoadError(f"Visual inference task failed: {e}") from e

        # 2. Parse and sanitize coordinate/text results
        return self.parse_and_sanitize(generated_text, image_size)

    @classmethod
    def parse_and_sanitize(
        cls, text: str, image_size: Tuple[int, int]
    ) -> Dict[str, Any]:
        """Parse coordinate coordinates, strip markup tags, normalize, and sanitize text.

        Parameters
        ----------
        text : str
            Raw textual output from Florence-2.
        image_size : tuple of (int, int)
            The (width, height) of the analyzed image.

        Returns
        -------
        dict
            Formatted parsed results.
        """
        width, height = image_size
        coordinates = []

        # Split output text by location box matches to isolate text labels
        matches = list(LOC_BOX_PATTERN.finditer(text))

        # We have len(matches) boxes, resulting in len(matches) + 1 textual segments.
        segments = []
        last_idx = 0
        for match in matches:
            segments.append(text[last_idx : match.start()])
            last_idx = match.end()
        segments.append(text[last_idx:])

        # Clean all segments
        clean_segs = [sanitize_text(s) for s in segments]
        used_segs = [False] * len(clean_segs)

        for i, match in enumerate(matches):
            ymin_raw, xmin_raw, ymax_raw, xmax_raw = map(int, match.groups())

            # Normalize coordinates into relative dimensions (0 to 1)
            ymin_rel = ymin_raw / 1000.0
            xmin_rel = xmin_raw / 1000.0
            ymax_rel = ymax_raw / 1000.0
            xmax_rel = xmax_raw / 1000.0

            # Scale to original image dimensions
            ymin_scaled = ymin_rel * height
            xmin_scaled = xmin_rel * width
            ymax_scaled = ymax_rel * height
            xmax_scaled = xmax_rel * width

            # Heuristically associate label from adjacent segments
            label = ""
            # Try preceding segment first if not empty, not already used, and doesn't end with a colon
            if (
                i < len(clean_segs)
                and clean_segs[i]
                and not clean_segs[i].endswith(":")
                and not used_segs[i]
            ):
                label = clean_segs[i]
                used_segs[i] = True
            # Otherwise try succeeding segment
            elif i + 1 < len(clean_segs) and clean_segs[i + 1] and not used_segs[i + 1]:
                label = clean_segs[i + 1]
                used_segs[i + 1] = True
            # Fallback to preceding even if it ends with a colon (stripping it) if we have nothing else
            elif i < len(clean_segs) and clean_segs[i] and not used_segs[i]:
                label = clean_segs[i].rstrip(":").strip()
                used_segs[i] = True
            else:
                label = "detected_object"

            coordinates.append(
                {
                    "box_2d_relative": [ymin_rel, xmin_rel, ymax_rel, xmax_rel],
                    "box_2d_scaled": [
                        ymin_scaled,
                        xmin_scaled,
                        ymax_scaled,
                        xmax_scaled,
                    ],
                    "label": label,
                }
            )

        sanitized_text = sanitize_text(text)

        return {
            "raw_output": text,
            "sanitized_text": sanitized_text,
            "coordinates": coordinates,
        }
