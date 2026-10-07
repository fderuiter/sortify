"""Sidecar tag storage manager for persisting custom document tags in .sortify_tags.json files."""

import json
import logging
from pathlib import Path
from typing import Dict, List, Union

logger = logging.getLogger(__name__)

SIDECAR_FILENAME = ".sortify_tags.json"


def get_sidecar_path(filepath: Union[str, Path]) -> Path:
    """Return the Path to the .sortify_tags.json sidecar file adjacent to target document."""
    path = Path(filepath).resolve()
    if path.is_dir():
        return path / SIDECAR_FILENAME
    return path.parent / SIDECAR_FILENAME


def parse_tags_input(tags_input: Union[str, List[str]]) -> List[str]:
    """Parse comma-separated string or list into a deduplicated list of trimmed non-empty tag strings."""
    if isinstance(tags_input, str):
        raw_tags = tags_input.split(",")
    else:
        raw_tags = tags_input

    result: List[str] = []
    seen = set()
    for tag in raw_tags:
        cleaned = str(tag).strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return result


def load_all_sidecar_tags_for_dir(dirpath: Union[str, Path]) -> Dict[str, List[str]]:
    """Load all tag mappings for files in target directory from .sortify_tags.json."""
    dir_path = Path(dirpath).resolve()
    sidecar_path = (
        dir_path / SIDECAR_FILENAME
        if dir_path.is_dir()
        else dir_path.parent / SIDECAR_FILENAME
    )

    if not sidecar_path.exists() or not sidecar_path.is_file():
        return {}

    try:
        with open(sidecar_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return {}

        file_tags_map: Dict[str, List[str]] = {}
        for filename, tag_value in data.items():
            if isinstance(tag_value, list):
                file_tags_map[filename] = parse_tags_input(tag_value)
            elif (
                isinstance(tag_value, dict)
                and "tags" in tag_value
                and isinstance(tag_value["tags"], list)
            ):
                file_tags_map[filename] = parse_tags_input(tag_value["tags"])
            elif isinstance(tag_value, str):
                file_tags_map[filename] = parse_tags_input(tag_value)

        return file_tags_map
    except Exception as e:
        logger.warning(f"Failed to read sidecar tags file at {sidecar_path}: {e}")
        return {}


def load_sidecar_tags(filepath: Union[str, Path]) -> List[str]:
    """Load tags for specific file from adjacent .sortify_tags.json sidecar file."""
    path = Path(filepath).resolve()
    filename = path.name
    tags_map = load_all_sidecar_tags_for_dir(path.parent)
    return tags_map.get(filename, [])


def save_sidecar_tags(
    filepath: Union[str, Path], tags: Union[str, List[str]]
) -> None:
    """Save tags for target file into adjacent .sortify_tags.json sidecar file."""
    path = Path(filepath).resolve()
    filename = path.name
    dir_path = path.parent
    sidecar_path = dir_path / SIDECAR_FILENAME

    parsed_tags = parse_tags_input(tags)

    # Load existing sidecar content if present
    data: Dict[str, Union[List[str], Dict[str, List[str]]]] = {}
    if sidecar_path.exists() and sidecar_path.is_file():
        try:
            with open(sidecar_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    data = loaded
        except Exception as e:
            logger.warning(
                f"Error reading existing sidecar at {sidecar_path} before save: {e}"
            )

    if parsed_tags:
        data[filename] = parsed_tags
    else:
        data.pop(filename, None)

    dir_path.mkdir(parents=True, exist_ok=True)

    temp_path = sidecar_path.with_suffix(".tmp")
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        temp_path.replace(sidecar_path)
    except Exception as e:
        logger.error(f"Failed to save sidecar tags to {sidecar_path}: {e}")
        if temp_path.exists():
            try:
                temp_path.unlink()
            except Exception:
                pass
        raise
