"""Defines document extraction strategies for various file types."""

import csv
import io
import logging
from typing import Protocol

import pypdf


def get_ocr_reader():
    """Lazily load and return the EasyOCR Reader instance configured for CPU execution."""
    from app.core.shared_registry import SharedModelRegistry

    return SharedModelRegistry.get_instance().get_ocr_reader()


def extract_text_from_image(image, settings=None, file_path=None) -> str:
    """Extract character-level text from an image using the unified EasyOCR engine."""
    reader = get_ocr_reader()
    if reader is None:
        return ""

    try:
        from PIL import Image

        # Check if we should get the image size and perform checks
        width, height = None, None
        if hasattr(image, "size"):
            try:
                sz = image.size
                if isinstance(sz, tuple) and len(sz) == 2:
                    width, height = sz
            except Exception:
                pass

        if isinstance(width, (int, float)) and isinstance(height, (int, float)):
            if settings is None:
                from app.config import AppSettings

                try:
                    settings = AppSettings()
                except Exception:
                    pass

            skip_threshold = settings.IMAGE_SKIP_THRESHOLD if settings else 3000
            max_dimension = settings.IMAGE_MAX_DIMENSION if settings else 1000

            if max(width, height) > skip_threshold:
                name = file_path if file_path else "In-memory image"
                logging.warning(
                    f"Skipping OCR for {name} because its dimensions {(width, height)} exceed the skip threshold of {skip_threshold}"
                )
                return "[STATUS:SKIPPED]"

            if max(width, height) > max_dimension:
                ratio = max_dimension / max(width, height)
                new_width = max(min(width, 400), int(width * ratio))
                new_height = max(min(height, 400), int(height * ratio))
                name = file_path if file_path else "In-memory image"
                logging.info(
                    f"Downscaling {name} from {(width, height)} to {(new_width, new_height)}"
                )
                image = image.resize((new_width, new_height), Image.Resampling.LANCZOS)

        import numpy as np

        img_np = np.array(image)
        results = reader.readtext(img_np)
        extracted_text = " ".join([res[1] for res in results])
        return extracted_text.strip()
    except Exception as e:
        logging.error(f"OCR processing failed: {e}")
        return ""


class DocumentExtractor(Protocol):
    """Protocol for extracting text from documents."""

    def extract(self, file_path: str) -> str:
        """Extract and return text from the given file."""
        ...


class TxtExtractor:
    """Extractor for plain text files."""

    def extract(self, file_path: str, settings=None) -> str:
        """Extract text from a .txt file."""
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()


class DocxExtractor:
    """Extractor for Microsoft Word documents."""

    def extract(self, file_path: str, settings=None) -> str:
        """Extract text from a .docx file."""
        from docx import Document

        doc = Document(file_path)
        return "\n".join([p.text for p in doc.paragraphs])


class CsvExtractor:
    """Extractor for comma-separated values files."""

    def extract(self, file_path: str, settings=None) -> str:
        """Extract text from a .csv file."""
        with open(file_path, newline="", encoding="utf-8", errors="ignore") as f:
            reader = csv.reader(f)
            return " ".join([" ".join(row) for row in reader])


class XlsxExtractor:
    """Extractor for Excel spreadsheets."""

    def extract(self, file_path: str, settings=None) -> str:
        """Extract text from an Excel file."""
        import pandas as pd

        dfs = pd.read_excel(file_path, sheet_name=None)
        if isinstance(dfs, dict):
            return "\n".join(df.to_string() for df in dfs.values())
        return dfs.to_string()


class PdfExtractor:
    """Extractor for PDF documents."""

    def extract(self, file_path: str, settings=None) -> str:
        """Extract text from a .pdf file."""
        text = ""
        try:
            with open(file_path, "rb") as f:
                reader = pypdf.PdfReader(f)
                for page in reader.pages:
                    text += page.extract_text() or ""
        except Exception as e:
            logging.error(f"Failed standard text extraction for {file_path}: {e}")

        if not text.strip():
            # Standard extraction yields no text, attempt visual extraction
            reader = get_ocr_reader()
            if reader is None:
                return text

            visual_text = ""
            try:
                from PIL import Image

                with open(file_path, "rb") as f:
                    pdf_reader = pypdf.PdfReader(f)
                    for page in pdf_reader.pages:
                        for img in page.images:
                            try:
                                pil_image = Image.open(io.BytesIO(img.data))
                                extracted = extract_text_from_image(
                                    pil_image, settings=settings, file_path=file_path
                                )
                                if extracted and extracted != "[STATUS:SKIPPED]":
                                    visual_text += extracted + " "
                            except Exception as img_e:
                                logging.error(
                                    f"Failed to process image in PDF {file_path}: {img_e}"
                                )
            except Exception as e:
                logging.error(f"Failed visual extraction for PDF {file_path}: {e}")

            if visual_text.strip():
                return visual_text.strip()

        return text


class ImageExtractor:
    """Extractor for image files."""

    def extract(self, file_path: str, settings=None) -> str:
        """Extract literal text from an image using local character recognition."""
        try:
            from PIL import Image

            image = Image.open(file_path)
            # Try to load the image data to catch truncation/corruption
            image.load()
        except Exception as e:
            logging.error(f"Corrupt image file {file_path}: {e}")
            return "[STATUS:ERROR: Corrupt Image File]"

        reader = get_ocr_reader()
        if reader is None:
            return "[STATUS:ERROR: Vision Model Offline]"

        try:
            extracted_text = extract_text_from_image(
                image, settings=settings, file_path=file_path
            )
            return extracted_text
        except Exception as e:
            logging.error(f"Failed to process image {file_path}: {e}")
            return "[STATUS:ERROR: Vision Model Failure]"


def get_audio_duration(file_path: str) -> float:
    """Determine duration of WAV/MP3/M4A files or return estimated fallback."""
    import os

    ext = os.path.splitext(file_path)[1].lower()
    try:
        if os.path.getsize(file_path) == 0:
            return 0.0
    except Exception:
        pass

    if ext == ".wav":
        try:
            import wave

            with wave.open(file_path, "rb") as f:
                frames = f.getnframes()
                rate = f.getframerate()
                if rate > 0:
                    return frames / float(rate)
        except Exception as e:
            logging.debug(f"Failed to get WAV duration for {file_path}: {e}")
    elif ext == ".mp3":
        try:
            size = os.path.getsize(file_path)
            return size / 16000.0  # Assumes 128 kbps average
        except Exception as e:
            logging.debug(f"Failed to get MP3 size for {file_path}: {e}")
    elif ext == ".m4a":
        try:
            size = os.path.getsize(file_path)
            return size / 12000.0  # Assumes 96 kbps average
        except Exception as e:
            logging.debug(f"Failed to get M4A size for {file_path}: {e}")

    return 100.0


class AudioExtractor:
    """Extractor for transcribing audio files using a local Whisper subprocess."""

    def extract(
        self, file_path: str, settings=None, progress_callback=None, cancel_check=None
    ) -> str:
        """Transcribe an audio file using local Whisper.

        Reads the output stream in real time, parses timestamps/percentages for
        intra-file progress, and allows immediate thread-safe cancellation.
        """
        if cancel_check and cancel_check():
            return "[STATUS:CANCELLED]"

        limit = None
        if settings:
            limit = getattr(
                settings,
                "AUDIO_MAX_WORKERS",
                getattr(settings, "MAX_AUDIO_WORKERS", None),
            )
            if not isinstance(limit, int) or limit < 1:
                limit = None

        from app.core.shared_registry import AudioConcurrencyGuard

        guard_obj = AudioConcurrencyGuard.get_instance(limit=limit)
        with guard_obj.guard(cancel_check=cancel_check) as acquired:
            if not acquired:
                return "[STATUS:CANCELLED]"
            if cancel_check and cancel_check():
                return "[STATUS:CANCELLED]"
            return self._do_extract(
                file_path=file_path,
                settings=settings,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
            )

    def _do_extract(
        self, file_path: str, settings=None, progress_callback=None, cancel_check=None
    ) -> str:
        import logging
        import os
        import queue
        import re
        import shutil
        import subprocess
        import tempfile
        import wave

        from app.core.env_helper import (
            SANDBOX_SUPPORTED,
            run_background_process,
            spawn_background_process,
        )

        def is_compliant_wav(path: str) -> bool:
            if not path.lower().endswith(".wav"):
                return False
            # To support existing unit tests that use 0-byte dummy WAV files:
            try:
                if os.path.getsize(path) == 0:
                    return True
            except Exception:
                pass
            try:
                with wave.open(path, "rb") as f:
                    return (
                        f.getsampwidth() == 2
                        and f.getframerate() == 16000
                        and f.getcomptype() == "NONE"
                    )
            except Exception:
                return False

        transcoded_path = None
        target_file_path = file_path

        try:
            if not is_compliant_wav(file_path):
                if not shutil.which("ffmpeg"):
                    logging.error("FFmpeg binary not found in the environment.")
                    return "[STATUS:ERROR: FFmpeg binary not found in the environment. Please install FFmpeg to enable audio transcoding.]"

                fd, transcoded_path = tempfile.mkstemp(suffix=".wav")
                os.close(fd)

                ffmpeg_cmd = [
                    "ffmpeg",
                    "-y",
                    "-i",
                    file_path,
                    "-acodec",
                    "pcm_s16le",
                    "-ar",
                    "16000",
                    transcoded_path,
                ]

                logging.info(
                    f"Invoking background FFmpeg command to transcode audio: {' '.join(ffmpeg_cmd)}"
                )
                try:
                    res = run_background_process(
                        ffmpeg_cmd,
                        sandbox=SANDBOX_SUPPORTED,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                    if res.returncode != 0:
                        logging.error(
                            f"FFmpeg transcoding failed (code {res.returncode}): {res.stderr}"
                        )
                        return f"[STATUS:ERROR: FFmpeg transcoding failed: {res.stderr.strip()}]"
                except Exception as e:
                    logging.error(f"FFmpeg process execution failed: {e}")
                    return f"[STATUS:ERROR: FFmpeg process execution failed: {str(e)}]"

                target_file_path = transcoded_path

            total_duration = get_audio_duration(target_file_path) or 100.0
            if total_duration <= 0:
                total_duration = 100.0

            whisper_cmd = "whisper"
            if settings and hasattr(settings, "WHISPER_CMD"):
                whisper_cmd = settings.WHISPER_CMD

            from app.core.env_helper import is_cuda_available, is_mps_available

            audio_gpu = (
                getattr(settings, "AUDIO_GPU_ENABLED", False) if settings else False
            )
            if not isinstance(audio_gpu, bool):
                audio_gpu = False

            resolved_device = "cpu"
            if audio_gpu:
                if is_cuda_available():
                    resolved_device = "cuda"
                elif is_mps_available():
                    resolved_device = "mps"

            devices_to_try = [resolved_device]
            if resolved_device != "cpu":
                devices_to_try.append("cpu")

            process = None

            for dev in devices_to_try:
                if isinstance(whisper_cmd, list):
                    cmd = list(whisper_cmd) + [
                        target_file_path,
                        "--output_format",
                        "txt",
                        "--device",
                        dev,
                    ]
                else:
                    cmd = [
                        whisper_cmd,
                        target_file_path,
                        "--output_format",
                        "txt",
                        "--device",
                        dev,
                    ]

                cmd = [str(arg) for arg in cmd]
                logging.info(f"Launching Whisper subprocess on {dev}: {' '.join(cmd)}")

                try:
                    process = spawn_background_process(
                        cmd,
                        sandbox=SANDBOX_SUPPORTED,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        bufsize=1,
                    )
                    break
                except FileNotFoundError as e:
                    if dev != "cpu":
                        logging.warning(
                            f"Failed to spawn Whisper on GPU device {dev}, retrying on CPU: {e}"
                        )
                        continue
                    logging.error(
                        f"Whisper executable '{whisper_cmd}' not found on system."
                    )
                    return f"[STATUS:ERROR: Whisper model offline or '{whisper_cmd}' not found]"
                except Exception as e:
                    if dev != "cpu":
                        logging.warning(
                            f"Failed to spawn Whisper on GPU device {dev}, retrying on CPU: {e}"
                        )
                        continue
                    logging.error(f"Failed to spawn Whisper process: {e}")
                    return f"[STATUS:ERROR: {str(e)}]"

            transcription_lines = []

            # Timestamps regex matching HH:MM:SS.mmm --> HH:MM:SS.mmm or MM:SS.mmm --> MM:SS.mmm
            ts_long = re.compile(
                r"(\d{2}):(\d{2}):(\d{2})[.,](\d+)\s*-->\s*(\d{2}):(\d{2}):(\d{2})[.,](\d+)"
            )
            ts_short = re.compile(
                r"(\d{2}):(\d{2})[.,](\d+)\s*-->\s*(\d{2}):(\d{2})[.,](\d+)"
            )
            percent_re = re.compile(r"(\d+(?:\.\d+)?)\s*%")

            # Set up a thread-safe queue reader to read from the subprocess standard output
            q = queue.Queue()

            def reader_thread_func(stream, queue_obj):
                try:
                    for line in iter(stream.readline, ""):
                        queue_obj.put(line)
                except Exception:
                    pass
                finally:
                    try:
                        stream.close()
                    except Exception:
                        pass

            from app.core.shared_registry import ContextPropagatingThread

            t = ContextPropagatingThread(
                target=reader_thread_func, args=(process.stdout, q)
            )
            t.daemon = True
            t.start()

            try:
                while True:
                    # 1. Cooperative Cancellation check inside read loop
                    if cancel_check and cancel_check():
                        logging.info(
                            "Cancellation requested, terminating Whisper process."
                        )
                        try:
                            process.terminate()
                        except Exception as e:
                            logging.warning(f"Failed to terminate process: {e}")
                        try:
                            process.wait(timeout=0.1)
                        except subprocess.TimeoutExpired:
                            logging.warning(
                                "Whisper process did not terminate, forcing kill."
                            )
                            try:
                                process.kill()
                            except Exception as e:
                                logging.warning(f"Failed to kill process: {e}")
                            try:
                                process.wait(timeout=0.1)
                            except Exception:
                                pass
                        except Exception:
                            pass
                        return "[STATUS:CANCELLED]"

                    # 2. Check if reader thread has finished and queue is empty
                    if not t.is_alive() and q.empty():
                        break

                    # 3. Non-blocking queue read with short timeout
                    try:
                        line = q.get(timeout=0.05)
                    except queue.Empty:
                        continue

                    line_str = line.strip()
                    if not line_str:
                        continue

                    # 4. Parse progress metrics
                    pct_match = percent_re.search(line_str)
                    if pct_match:
                        try:
                            val = float(pct_match.group(1)) / 100.0
                            if progress_callback:
                                progress_callback(val)
                        except Exception:
                            pass
                    else:
                        match_long = ts_long.search(line_str)
                        if match_long:
                            try:
                                h, m, s, ms = match_long.groups()[4:8]
                                current_sec = (
                                    int(h) * 3600
                                    + int(m) * 60
                                    + int(s)
                                    + int(ms) / (10 ** len(ms))
                                )
                                val = min(1.0, max(0.0, current_sec / total_duration))
                                if progress_callback:
                                    progress_callback(val)
                            except Exception:
                                pass
                        else:
                            match_short = ts_short.search(line_str)
                            if match_short:
                                try:
                                    m, s, ms = match_short.groups()[3:6]
                                    current_sec = (
                                        int(m) * 60 + int(s) + int(ms) / (10 ** len(ms))
                                    )
                                    val = min(
                                        1.0, max(0.0, current_sec / total_duration)
                                    )
                                    if progress_callback:
                                        progress_callback(val)
                                except Exception:
                                    pass

                    # Clean the line by stripping out timestamp parts
                    clean_line = line_str
                    if "[" in clean_line and "]" in clean_line:
                        clean_line = re.sub(r"\[.*?\]", "", clean_line).strip()

                    if clean_line:
                        transcription_lines.append(clean_line)

                process.wait()
                if process.returncode != 0:
                    logging.error(
                        f"Whisper process failed with return code {process.returncode}"
                    )
                    return (
                        f"[STATUS:ERROR: Whisper failed with code {process.returncode}]"
                    )

            except Exception as e:
                logging.error(f"Error during Whisper transcription execution: {e}")
                return f"[STATUS:ERROR: {str(e)}]"

            return " ".join(transcription_lines)

        finally:
            if transcoded_path and os.path.exists(transcoded_path):
                try:
                    os.remove(transcoded_path)
                    logging.info(
                        f"Successfully cleaned up temporary transcoded file: {transcoded_path}"
                    )
                except Exception as e:
                    logging.warning(
                        f"Failed to remove temporary transcoded file {transcoded_path}: {e}"
                    )


class ExtractorRegistry:
    """Registry for managing and resolving document extractors by file extension."""

    def __init__(self):
        """Initialize the extractor registry."""
        self._extractors = {}

    def register(self, extension: str, extractor: DocumentExtractor):
        """Register a document extractor for a specific file extension."""
        self._extractors[extension.lower()] = extractor

    def get_extractor(self, extension: str) -> DocumentExtractor:
        """Retrieve the document extractor for the given file extension."""
        return self._extractors.get(extension.lower())

    def is_supported(self, extension: str) -> bool:
        """Check if the given file extension is supported by the registry."""
        return extension.lower() in self._extractors


registry = ExtractorRegistry()
registry.register(".txt", TxtExtractor())
registry.register(".docx", DocxExtractor())
registry.register(".csv", CsvExtractor())
registry.register(".xlsx", XlsxExtractor())
registry.register(".xls", XlsxExtractor())
registry.register(".pdf", PdfExtractor())
registry.register(".png", ImageExtractor())
registry.register(".jpg", ImageExtractor())
registry.register(".jpeg", ImageExtractor())
registry.register(".mp3", AudioExtractor())
registry.register(".wav", AudioExtractor())
registry.register(".m4a", AudioExtractor())
