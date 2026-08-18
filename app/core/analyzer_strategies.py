"""Defines clustering strategies for grouping documents."""

import contextvars
import functools
import logging
import multiprocessing
import os
import sys
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import List, Protocol


def is_debug_active() -> bool:
    """Check if debug mode is active via the DEBUG environment variable."""
    return os.environ.get("DEBUG", "").strip().lower() in ("1", "true", "yes", "on")


def is_prompt_dump_enabled() -> bool:
    """Check if prompt dumping is enabled (requires active debug mode and PROMPT_DUMP_FILE)."""
    return is_debug_active() and bool(os.environ.get("PROMPT_DUMP_FILE"))


ILLEGAL_DUMP_PATH_CHARS = set('<>?*|"\0')


def validate_prompt_dump_path(dump_file: str) -> None:
    """Validate requested dump path to prevent directory traversal and illegal characters.

    Raises
    ------
        ValueError: If dump_file path contains relative traversal sequences or illegal characters.
    """
    if not isinstance(dump_file, str) or not dump_file.strip():
        raise ValueError("Prompt dump target path must be a non-empty string.")

    if any(c in ILLEGAL_DUMP_PATH_CHARS for c in dump_file):
        raise ValueError(
            f"Invalid prompt dump path '{dump_file}': contains illegal characters."
        )

    # Check for relative directory traversal segments ('..')
    normalized_path = dump_file.replace("\\", "/")
    segments = normalized_path.split("/")
    if ".." in segments:
        raise ValueError(
            f"Invalid prompt dump path '{dump_file}': relative directory traversal ('..') is prohibited."
        )


def scrub_prompt_text(text: str) -> str:
    """Scrub user home directory paths from prompt text prior to writing to disk."""
    if not isinstance(text, str) or not text:
        return text

    try:
        home_dir = str(Path.home())
    except Exception:
        home_dir = None

    if home_dir and home_dir != "/":
        home_dir_fwd = home_dir.replace("\\", "/")
        home_dir_back = home_dir.replace("/", "\\")
        text = text.replace(home_dir_fwd, "<USER_HOME>")
        text = text.replace(home_dir_back, "<USER_HOME>")

    return text


_DECRYPTION_EXECUTOR = None
_DECRYPTION_EXECUTOR_LOCK = threading.Lock()


def get_decryption_executor():
    """Retrieve or initialize the global thread pool executor for parallel decryption."""
    global _DECRYPTION_EXECUTOR
    if _DECRYPTION_EXECUTOR is None:
        with _DECRYPTION_EXECUTOR_LOCK:
            if _DECRYPTION_EXECUTOR is None:
                max_workers = min(32, (os.cpu_count() or 1) + 4)
                _DECRYPTION_EXECUTOR = ThreadPoolExecutor(
                    max_workers=max_workers, thread_name_prefix="decryption_worker"
                )
    return _DECRYPTION_EXECUTOR


class IsolatedStrategyMixin:
    """Mixin class to isolate execution parameters, target paths, and active document maps to the executing thread/context."""

    _THREAD_ISOLATED_ATTRIBUTES = {
        "stop_words",
        "max_folders",
        "max_depth",
        "max_features",
        "_error",
        "_vector_map",
        "db",
        "base_dir",
        "pre_fetched_corpus",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        object.__setattr__(
            self,
            "_local_state_var",
            contextvars.ContextVar(f"state_{id(self)}", default={}),
        )

    def _get_local_state(self):
        if not hasattr(self, "_local_state_var"):
            object.__setattr__(
                self,
                "_local_state_var",
                contextvars.ContextVar(f"state_{id(self)}", default={}),
            )
        return self._local_state_var.get()

    def _set_local_state(self, state):
        if not hasattr(self, "_local_state_var"):
            object.__setattr__(
                self,
                "_local_state_var",
                contextvars.ContextVar(f"state_{id(self)}", default={}),
            )
        self._local_state_var.set(state)

    def __getattribute__(self, name):
        """Get attribute, routing thread-isolated variables to contextvars."""
        if name in object.__getattribute__(self, "_THREAD_ISOLATED_ATTRIBUTES"):
            state = object.__getattribute__(self, "_get_local_state")()
            if name in state:
                return state[name]
            try:
                return object.__getattribute__(self, name)
            except AttributeError:
                raise AttributeError(
                    f"'{type(self).__name__}' object has no attribute '{name}'"
                )
        return object.__getattribute__(self, name)

    def __setattr__(self, name, value):
        """Set attribute, routing thread-isolated variables to contextvars."""
        if name in getattr(self, "_THREAD_ISOLATED_ATTRIBUTES", set()):
            state = dict(self._get_local_state())
            state[name] = value
            self._set_local_state(state)
        else:
            object.__setattr__(self, name, value)

    def __delattr__(self, name):
        """Delete attribute, routing thread-isolated variables to contextvars."""
        if name in getattr(self, "_THREAD_ISOLATED_ATTRIBUTES", set()):
            state = dict(self._get_local_state())
            if name in state:
                del state[name]
                self._set_local_state(state)
            else:
                try:
                    object.__delattr__(self, name)
                except AttributeError:
                    raise AttributeError(
                        f"'{type(self).__name__}' object has no attribute '{name}'"
                    )
        else:
            object.__delattr__(self, name)

    def clear_isolated_state(self):
        """Clean up the thread-isolated context variables to prevent memory leaks."""
        self._set_local_state({})


def thread_isolated_execution(func):
    """Track execution depth and automatically clean up isolated state at the outermost call."""

    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        import threading

        if not hasattr(self, "_local_state_var"):
            object.__setattr__(
                self,
                "_local_state_var",
                contextvars.ContextVar(f"state_{id(self)}", default={}),
            )
        state = self._local_state_var.get()
        depth = state.get("_execution_depth", 0)

        state = dict(state)
        state["_execution_depth"] = depth + 1
        self._local_state_var.set(state)

        try:
            return func(self, *args, **kwargs)
        finally:
            state = dict(self._local_state_var.get())
            current_depth = state.get("_execution_depth", 1) - 1
            if current_depth <= 0:
                if threading.current_thread() is not threading.main_thread():
                    self._local_state_var.set({})
                else:
                    state["_execution_depth"] = 0
                    self._local_state_var.set(state)
            else:
                state["_execution_depth"] = current_depth
                self._local_state_var.set(state)

    return wrapper


LANGUAGE_CHAR_MAP = {
    "en": "a-zA-Z0-9",
    "de": "a-zA-Z0-9äöüÄÖÜß",
    "fr": "a-zA-Z0-9âàæçéèêëîïôœùûüÿÂÀÆÇÉÈÊËÎÏÔŒÙÛÜŸ",
    "es": "a-zA-Z0-9áéíóúüñÁÉÍÓÚÜÑ",
    "it": "a-zA-Z0-9àèéìíîòóùúÀÈÉÌÍÎÒÓÙÚ",
    "pt": "a-zA-Z0-9áâãàçéêíóôõúÁÂÃÀÇÉÊÍÓÔÕÚ",
    "ru": "a-zA-Z0-9а-яА-ЯёЁ",
    "uk": "a-zA-Z0-9а-яА-ЯёЁіІїЇєЄґҐ",
    "bg": "a-zA-Z0-9а-яА-ЯёЁ",
    "el": "a-zA-Z0-9α-ωΑ-ΩάέίόύώήΆΈΊΌΎΏΉϊϋΐΰ",
    "ch_sim": "a-zA-Z0-9\u4e00-\u9fff",
    "ch_tra": "a-zA-Z0-9\u4e00-\u9fff",
    "ja": "a-zA-Z0-9\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fff",
    "ko": "a-zA-Z0-9\uac00-\ud7af",
}


@contextmanager
def block_external_network():
    """Block outgoing non-localhost network traffic during naming generation."""
    from app.core.shared_registry import (
        block_external_network as _block_external_network,
    )

    with _block_external_network(reason="folder naming"):
        yield


def recursive_kmeans_worker_main(
    filenames: list,
    documents: list,
    max_folders: int,
    stop_words: set,
    max_depth: int,
    max_features: int,
    pre_fetched_vectors: list | None,
    output_queue,
    strategy_class_name: str = "RecursiveKMeansStrategy",
    thread_limit: int | None = None,
    pre_fetched_corpus: list | dict | None = None,
):
    """Worker process main loop that handles core recursive KMeans mathematical clustering calculations."""
    import logging
    import os
    import sys

    # 1. Respect configured CPU thread limits from global registry
    if thread_limit is None:
        try:
            from app.core.shared_registry import SharedModelRegistry

            thread_limit = SharedModelRegistry.get_instance().get_thread_limit()
        except Exception:
            thread_limit = 2

    # Set thread limits for all math/vector libraries
    limit_str = str(thread_limit)
    os.environ["OMP_NUM_THREADS"] = limit_str
    os.environ["MKL_NUM_THREADS"] = limit_str
    os.environ["OPENBLAS_NUM_THREADS"] = limit_str
    os.environ["VECLIB_MAXIMUM_THREADS"] = limit_str
    os.environ["NUMEXPR_NUM_THREADS"] = limit_str

    try:
        import torch

        torch.set_num_threads(thread_limit)
    except Exception:
        pass

    # 2. Priority management
    try:
        from app.core.semantic_embeddings import set_low_priority

        set_low_priority()
    except Exception:
        pass

    if sys.platform != "win32":
        try:
            os.nice(19)
        except Exception:
            pass

    # 3. Create the appropriate strategy instance and execute calculations
    strategy = None
    try:
        strategy_cls = globals().get(strategy_class_name)
        if strategy_cls is not None:
            strategy = strategy_cls()
        else:
            strategy = RecursiveKMeansStrategy()

        # Prevent DB access in child process
        strategy.db = None
        strategy.base_dir = None
        strategy.pre_fetched_corpus = pre_fetched_corpus

        strategy.stop_words = stop_words
        strategy.max_folders = max_folders
        strategy.max_depth = max_depth
        strategy.max_features = max_features
        strategy._error = 0.0

        if pre_fetched_vectors is not None:
            strategy._vector_map = {
                f: v for f, v in zip(filenames, pre_fetched_vectors)
            }
        else:
            strategy._vector_map = {}

        # Perform the actual clustering calculation
        plan = strategy._cluster_recursive(filenames, documents, depth=1)

        worker_pid = os.getpid()
        worker_niceness = None
        if sys.platform != "win32":
            try:
                worker_niceness = os.nice(0)
            except Exception:
                pass

        output_queue.put(
            {
                "status": "success",
                "plan": plan,
                "error": strategy._error,
                "worker_pid": worker_pid,
                "worker_niceness": worker_niceness,
                "worker_thread_limit": thread_limit,
            }
        )
    except Exception as e:
        import traceback

        logging.error(
            f"Error inside clustering child process: {e}\n{traceback.format_exc()}"
        )
        output_queue.put({"status": "error", "message": str(e)})
    finally:
        # Data Boundary Safeguard: clear decrypted data immediately after folder naming concludes
        if strategy is not None:
            strategy.pre_fetched_corpus = None
        pre_fetched_corpus = None


class ClusteringStrategy(Protocol):
    """Protocol for defining document clustering strategies."""

    def generate_plan(
        self,
        filenames: List[str],
        documents: List[str],
        max_folders: int,
        stop_words: set,
        max_depth: int = 5,
        max_features: int = 3,
        pre_fetched_vectors: List[list] | None = None,
        cancel_check=None,
        pre_fetched_corpus: list | dict | None = None,
    ) -> tuple[dict, float]:
        """Return the clustering plan and the total reconstruction error."""
        ...


class RecursiveKMeansStrategy(IsolatedStrategyMixin):
    """Strategy that uses recursive KMeans to cluster documents."""

    def _generate_plan_inline(
        self,
        filenames: List[str],
        documents: List[str],
        max_folders: int,
        stop_words: set,
        max_depth: int = 5,
        max_features: int = 3,
        pre_fetched_vectors: List[list] | None = None,
    ) -> tuple[dict, float]:
        """Original inline implementation of clustering."""
        self.stop_words = stop_words
        self.max_folders = max_folders
        self.max_depth = max_depth
        self.max_features = max_features
        self._error = 0.0

        if pre_fetched_vectors is not None:
            self._vector_map = {f: v for f, v in zip(filenames, pre_fetched_vectors)}
        else:
            self._vector_map = {}

        plan = self._cluster_recursive(filenames, documents, depth=1)
        return plan, self._error

    @thread_isolated_execution
    def generate_plan(
        self,
        filenames: List[str],
        documents: List[str],
        max_folders: int,
        stop_words: set,
        max_depth: int = 5,
        max_features: int = 3,
        pre_fetched_vectors: List[list] | None = None,
        cancel_check=None,
        pre_fetched_corpus: list | dict | None = None,
    ) -> tuple[dict, float]:
        """Return a hierarchical clustering plan and error using KMeans by delegating to a separate child process."""
        self.stop_words = stop_words
        self.max_folders = max_folders
        self.max_depth = max_depth
        self.max_features = max_features
        self._error = 0.0
        self.pre_fetched_corpus = pre_fetched_corpus

        if pre_fetched_vectors is not None:
            self._vector_map = {f: v for f, v in zip(filenames, pre_fetched_vectors)}
        else:
            self._vector_map = {}

        # Bypass multiprocessing during normal pytest run to allow mocking/assertions on instance state
        import os

        if "PYTEST_CURRENT_TEST" in os.environ and not os.environ.get(
            "FORCE_MULTIPROCESSING_CLUSTERING"
        ):
            return self._generate_plan_inline(
                filenames,
                documents,
                max_folders,
                stop_words,
                max_depth,
                max_features,
                pre_fetched_vectors,
            )

        import logging
        import multiprocessing
        import queue
        import time

        # Retrieve the thread limit from the parent process global registry
        try:
            from app.core.shared_registry import SharedModelRegistry

            parent_thread_limit = SharedModelRegistry.get_instance().get_thread_limit()
        except Exception:
            parent_thread_limit = None

        ctx = multiprocessing.get_context("spawn")
        output_queue = ctx.Queue()

        strategy_class_name = self.__class__.__name__

        process = ctx.Process(
            target=recursive_kmeans_worker_main,
            args=(
                filenames,
                documents,
                max_folders,
                stop_words,
                max_depth,
                max_features,
                pre_fetched_vectors,
                output_queue,
                strategy_class_name,
            ),
            kwargs={
                "thread_limit": parent_thread_limit,
                "pre_fetched_corpus": pre_fetched_corpus,
            },
        )
        process.start()

        result = None
        poll_interval = 0.01

        try:
            while True:
                if cancel_check is not None and cancel_check():
                    logging.info(
                        "Clustering cancellation requested. Terminating isolated child process..."
                    )
                    if process.is_alive():
                        process.terminate()
                        process.join(timeout=2.0)
                        if process.is_alive():
                            process.kill()
                            process.join(timeout=0.1)
                    return {}, 0.0

                try:
                    result = output_queue.get_nowait()
                    break
                except queue.Empty:
                    pass

                if not process.is_alive():
                    try:
                        result = output_queue.get_nowait()
                    except queue.Empty:
                        pass
                    break

                time.sleep(poll_interval)
        except Exception as e:
            logging.error(f"Error while waiting for clustering child process: {e}")
            if process.is_alive():
                process.terminate()
                process.join(timeout=2.0)
                if process.is_alive():
                    process.kill()
            raise e

        if process.is_alive():
            process.terminate()
            process.join(timeout=2.0)
            if process.is_alive():
                process.kill()

        try:
            process.close()
        except Exception:
            pass

        if result is None:
            logging.warning(
                "Clustering child process did not return any result. Falling back to inline execution."
            )
            return self._generate_plan_inline(
                filenames,
                documents,
                max_folders,
                stop_words,
                max_depth,
                max_features,
                pre_fetched_vectors,
            )

        if result.get("status") == "success":
            self._last_worker_pid = result.get("worker_pid")
            self._last_worker_niceness = result.get("worker_niceness")
            self._last_worker_thread_limit = result.get("worker_thread_limit")
            return result["plan"], result["error"]
        else:
            logging.error(
                f"Clustering child process failed: {result.get('message')}. Falling back to inline execution."
            )
            return self._generate_plan_inline(
                filenames,
                documents,
                max_folders,
                stop_words,
                max_depth,
                max_features,
                pre_fetched_vectors,
            )

    def _get_cluster_keywords(self, documents: list) -> str:
        if not documents:
            return "Miscellaneous"
        try:
            from sklearn.feature_extraction.text import TfidfVectorizer

            vectorizer = TfidfVectorizer(
                stop_words=list(self.stop_words), max_features=self.max_features
            )
            X = vectorizer.fit_transform(documents)
            feature_names = vectorizer.get_feature_names_out()
            if len(feature_names) == 0:
                return "Miscellaneous"

            import numpy as np

            scores = np.asarray(X.sum(axis=0)).ravel()
            top_indices = scores.argsort()[::-1][:2]
            top_terms = [feature_names[i].capitalize() for i in top_indices]
            return "-".join(top_terms)
        except Exception:
            return "Miscellaneous"

    def _cluster_recursive(self, filenames: list, documents: list, depth: int) -> dict:
        plan = {}

        if depth >= self.max_depth or len(documents) < 3:
            for f in filenames:
                plan[f] = {
                    "__type__": "file",
                    "relative_source": f,
                    "source_path": f,
                    "routed_by": "clustering",
                }
            return {"Miscellaneous": plan} if depth == 1 else plan

        use_dense_vectors = False
        if getattr(self, "_vector_map", None):
            try:
                import numpy as np

                # Filter valid vectors (non-None elements)
                valid_vectors = [
                    self._vector_map[f]
                    for f in filenames
                    if self._vector_map.get(f) is not None
                ]

                # We must only fall back to lexical TF-IDF clustering if 100% of the documents in the partition lack valid embeddings
                if len(valid_vectors) > 0:
                    dimension = len(valid_vectors[0])
                    X_list = []
                    for f in filenames:
                        v = self._vector_map.get(f)
                        if v is not None:
                            X_list.append(v)
                        else:
                            # Generate a zero-filled vector of the exact matching dimension for any missing document embedding
                            zero_vector = [0.0] * dimension
                            X_list.append(zero_vector)

                    X = np.array(X_list)
                    use_dense_vectors = True
            except Exception as e:
                logging.error(
                    f"Failed to prepare dense vectors for clustering step: {e}"
                )
                use_dense_vectors = False

        if not use_dense_vectors:
            try:
                from sklearn.feature_extraction.text import TfidfVectorizer

                vectorizer = TfidfVectorizer(
                    stop_words=list(self.stop_words), max_features=1000
                )
                X = vectorizer.fit_transform(documents)
            except Exception:
                for f in filenames:
                    plan[f] = {
                        "__type__": "file",
                        "relative_source": f,
                        "source_path": f,
                        "routed_by": "clustering",
                    }
                return {"Miscellaneous": plan} if depth == 1 else plan

        actual_k = min(self.max_folders, len(documents) // 2)
        if actual_k < 2:
            actual_k = 2

        from sklearn.cluster import MiniBatchKMeans

        kmeans = MiniBatchKMeans(n_clusters=actual_k, random_state=42, n_init="auto")
        labels = kmeans.fit_predict(X)
        self._error += kmeans.inertia_

        topic_groups = defaultdict(list)
        for i, label in enumerate(labels):
            topic_groups[label].append((filenames[i], documents[i]))

        for topic_idx, group in topic_groups.items():
            sub_filenames = [item[0] for item in group]
            sub_documents = [item[1] for item in group]

            folder_name = self._get_cluster_keywords(sub_documents)

            from app.core.path_utils import sanitize_name

            folder_name = sanitize_name(folder_name)

            if len(group) == len(documents):
                for f in sub_filenames:
                    if folder_name not in plan:
                        plan[folder_name] = {}
                    plan[folder_name][f] = {
                        "__type__": "file",
                        "relative_source": f,
                        "source_path": f,
                        "routed_by": "clustering",
                    }
            else:
                sub_plan = self._cluster_recursive(
                    sub_filenames, sub_documents, depth + 1
                )

                def deep_update(d, u):
                    for k, v in u.items():
                        if isinstance(v, dict) and k in d and isinstance(d[k], dict):
                            deep_update(d[k], v)
                        else:
                            d[k] = v

                if folder_name not in plan:
                    plan[folder_name] = sub_plan
                else:
                    deep_update(plan[folder_name], sub_plan)

        return plan


def is_gguf_model_dir(model_path: str) -> bool:
    """Check recursively if the given path contains any GGUF files.

    Parameters
    ----------
    model_path : str
        The folder path to scan recursively.

    Returns
    -------
    bool
        True if at least one GGUF file is found, False otherwise.
    """
    if not model_path or not os.path.exists(model_path):
        return False
    for root, _, files in os.walk(model_path):
        for file in files:
            if file.lower().endswith(".gguf"):
                return True
    return False


def gguf_worker_main(model_path, input_queue, output_queue, n_threads=None):
    """Worker process main loop that handles local GGUF model generation.

    Parameters
    ----------
    model_path : str
        The folder path containing the GGUF model files.
    input_queue : multiprocessing.Queue
        Queue to receive tasks from the main process.
    output_queue : multiprocessing.Queue
        Queue to send results back to the main process.
    n_threads : int, optional
        Number of CPU threads to allocate for the model. If None, resolves from the registry thread limit.
    """
    import os

    gguf_file = None
    for root, _, files in os.walk(model_path):
        for file in files:
            if file.lower().endswith(".gguf"):
                gguf_file = os.path.join(root, file)
                break
        if gguf_file:
            break

    if not gguf_file:
        output_queue.put({"error": "No .gguf file found"})
        return

    try:
        from llama_cpp import Llama

        if n_threads is None:
            try:
                from app.core.shared_registry import SharedModelRegistry

                n_threads = SharedModelRegistry.get_instance().get_thread_limit()
            except Exception:
                import multiprocessing

                n_threads = os.cpu_count() or multiprocessing.cpu_count() or 2

        llm = Llama(
            model_path=gguf_file, n_ctx=2048, verbose=False, n_threads=n_threads
        )
        output_queue.put({"status": "ready"})
    except Exception as e:
        output_queue.put({"error": str(e)})
        return

    while True:
        try:
            task = input_queue.get()
            if task is None:
                break

            prompt = task.get("prompt", "")
            max_tokens = task.get("max_tokens", 15)
            grammar_str = task.get("grammar")

            grammar = None
            if grammar_str:
                try:
                    from llama_cpp import LlamaGrammar

                    grammar = LlamaGrammar.from_string(grammar_str)
                except Exception as e:
                    import logging

                    logging.error(
                        f"Failed to compile grammar constraint: {e}. Falling back to default ASCII grammar."
                    )
                    try:
                        from llama_cpp import LlamaGrammar

                        default_ascii_grammar = 'root ::= word (" " word)? (" " word)? (" " word)?\nword ::= [a-zA-Z0-9]+'
                        grammar = LlamaGrammar.from_string(default_ascii_grammar)
                    except Exception as fe:
                        logging.error(
                            f"Failed to compile default ASCII fallback grammar: {fe}"
                        )
                        grammar = None

            if grammar:
                try:
                    res = llm(
                        prompt, max_tokens=max_tokens, echo=False, grammar=grammar
                    )
                except Exception as e:
                    import logging

                    logging.error(f"Generation with grammar failed: {e}")
                    res = llm(prompt, max_tokens=max_tokens, echo=False)
            else:
                res = llm(prompt, max_tokens=max_tokens, echo=False)

            generated_text = res["choices"][0]["text"].strip()
            output_queue.put({"text": generated_text})
        except Exception as e:
            output_queue.put({"error": str(e)})


try:
    from transformers import LogitsProcessor, LogitsProcessorList
except ImportError:

    class LogitsProcessor:
        """Fallback LogitsProcessor class when transformers is not available."""

        pass

    class LogitsProcessorList(list):
        """Fallback LogitsProcessorList list class when transformers is not available."""

        pass


class NegativeLogitBiasProcessor(LogitsProcessor):
    """LogitsProcessor that applies negative logit biases to specified token IDs."""

    def __init__(self, token_biases: dict):
        self.token_biases = token_biases

    def __call__(self, input_ids, scores):
        """Apply negative logit biases to the specified tokens."""
        for token_id, bias in self.token_biases.items():
            if token_id < scores.shape[-1]:
                if len(scores.shape) == 1:
                    scores[token_id] += bias
                else:
                    scores[:, token_id] += bias
        return scores


def cooperative_queue_get(q, timeout=8.0):
    """Retrieve an item from a queue using a non-blocking cooperative polling loop."""
    import queue
    import time

    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            return q.get_nowait()
        except queue.Empty:
            pass
        time.sleep(0.01)  # Cooperative sleep to yield control to other threads / GIL
    raise queue.Empty


def cooperative_join(target, timeout=1.0):
    """Join a thread or process blockingly with cooperative GIL yielding to ensure complete termination."""
    import time

    start_time = time.time()
    is_alive_fn = getattr(target, "is_alive", None)
    if not is_alive_fn:
        if hasattr(target, "join"):
            target.join(timeout)
        return

    while is_alive_fn() and (time.time() - start_time < timeout):
        time.sleep(0.01)


class GenerativeNamingStrategy(RecursiveKMeansStrategy):
    """Strategy that uses a generative model to create descriptive folder names."""

    @thread_isolated_execution
    def generate_plan(
        self,
        filenames: List[str],
        documents: List[str],
        max_folders: int,
        stop_words: set,
        max_depth: int = 5,
        max_features: int = 3,
        pre_fetched_vectors: List[list] | None = None,
        cancel_check=None,
        pre_fetched_corpus: list | dict | None = None,
    ) -> tuple[dict, float]:
        """Generate a hierarchical plan of folder names using generative modeling."""
        plan, error = super().generate_plan(
            filenames,
            documents,
            max_folders,
            stop_words,
            max_depth,
            max_features,
            pre_fetched_vectors,
            cancel_check=cancel_check,
            pre_fetched_corpus=pre_fetched_corpus,
        )

        # Retrieve configurable similarity threshold
        threshold = getattr(self, "threshold", None)
        if threshold is None:
            settings_obj = getattr(self, "settings", None)
            if settings_obj is not None:
                threshold = getattr(settings_obj, "COHERENCE_THRESHOLD", 0.5)
            else:
                try:
                    from app.config import AppSettings

                    settings = AppSettings()
                    threshold = getattr(settings, "COHERENCE_THRESHOLD", 0.5)
                except Exception:
                    threshold = 0.5

        # 1. Gather all document vectors in filenames
        import numpy as np

        vector_dict = {}
        use_dense = False

        if getattr(self, "_vector_map", None):
            valid_vectors = {f: v for f, v in self._vector_map.items() if v is not None}
            if len(valid_vectors) > 0:
                vector_dict = {f: v for f, v in self._vector_map.items()}
                use_dense = True

        db = getattr(self, "db", None)
        base_dir = getattr(self, "base_dir", None)
        if not use_dense and db and base_dir:
            try:
                from app.core.semantic_embeddings import SemanticEmbeddingManager

                embedding_manager = SemanticEmbeddingManager(
                    db, model_path=getattr(self, "model_path", None)
                )
                if (
                    not embedding_manager.is_mock
                    and not embedding_manager.is_reconstruction_active()
                ):
                    fetched = embedding_manager.get_vectors_batch(
                        base_dir, filenames, regenerate=False
                    )
                    if fetched:
                        vector_dict = {
                            f: v for f, v in fetched.items() if v is not None
                        }
                        if len(vector_dict) > 0:
                            use_dense = True
            except Exception as e:
                logging.error(
                    f"Failed to fetch vectors in GenerativeNamingStrategy: {e}"
                )

        # Fallback to TF-IDF if dense vectors are not available/mock
        if not use_dense:
            try:
                from sklearn.feature_extraction.text import TfidfVectorizer

                vectorizer = TfidfVectorizer(
                    stop_words=list(stop_words), max_features=1000
                )
                safe_docs = [doc or "" for doc in documents]
                X = vectorizer.fit_transform(safe_docs)
                for i, f in enumerate(filenames):
                    vector_dict[f] = X[i].toarray()[0]
            except Exception as e:
                logging.error(f"Failed to generate TF-IDF vectors: {e}")
                for f in filenames:
                    vector_dict[f] = [0.0] * 384

        def get_cosine_similarity(vec1, vec2):
            v1 = np.array(vec1)
            v2 = np.array(vec2)
            norm1 = np.linalg.norm(v1)
            norm2 = np.linalg.norm(v2)
            if norm1 == 0 or norm2 == 0:
                return 0.0
            return float(np.dot(v1, v2) / (norm1 * norm2))

        def get_recursive_files(n):
            res = []
            for key, val in n.items():
                if (
                    val is None
                    or not isinstance(val, dict)
                    or val.get("__type__") == "file"
                ):
                    res.append(key)
                else:
                    res.extend(get_recursive_files(val))
            return res

        def filter_plan_vector(node):
            new_node = {}
            low_confidence_files = {}

            # Separate files and subfolders in this node
            files = []
            subfolders = {}
            for k, v in node.items():
                if (
                    v is None
                    or not isinstance(v, dict)
                    or (isinstance(v, dict) and v.get("__type__") == "file")
                ):
                    files.append((k, v))
                else:
                    subfolders[k] = v

            # 1. Process subfolders recursively
            for folder_name, folder_content in subfolders.items():
                filtered_sub, lc_sub = filter_plan_vector(folder_content)
                if filtered_sub:
                    new_node[folder_name] = filtered_sub
                low_confidence_files.update(lc_sub)

            # 2. Process files in current folder node
            if files:
                all_recursive_files = get_recursive_files(node)
                vectors = [
                    vector_dict[f]
                    for f in all_recursive_files
                    if f in vector_dict and vector_dict[f] is not None
                ]
                if vectors:
                    centroid = np.array(np.mean(vectors, axis=0), dtype=np.float64)
                    centroid_norm = float(np.linalg.norm(centroid))
                    for f, f_val in files:
                        leaf_info = (
                            f_val
                            if isinstance(f_val, dict) and f_val.get("__type__") == "file"
                            else {
                                "__type__": "file",
                                "relative_source": f,
                                "source_path": f,
                                "routed_by": "clustering",
                            }
                        )
                        f_vec = vector_dict.get(f)
                        if f_vec is not None:
                            if centroid_norm == 0:
                                sim = 0.0
                            else:
                                f_arr = np.array(f_vec, dtype=np.float64)
                                f_norm = float(np.linalg.norm(f_arr))
                                if f_norm == 0:
                                    sim = 0.0
                                else:
                                    sim = float(np.dot(f_arr, centroid) / (f_norm * centroid_norm))
                            if sim < threshold:
                                low_confidence_files[f] = leaf_info
                            else:
                                new_node[f] = leaf_info
                        else:
                            new_node[f] = leaf_info
                else:
                    for f, f_val in files:
                        leaf_info = (
                            f_val
                            if isinstance(f_val, dict) and f_val.get("__type__") == "file"
                            else {
                                "__type__": "file",
                                "relative_source": f,
                                "source_path": f,
                                "routed_by": "clustering",
                            }
                        )
                        new_node[f] = leaf_info

            return new_node, low_confidence_files

        # Run vector similarity filtering asynchronously (completely avoiding generative validation prompts)
        new_plan, lc_files = filter_plan_vector(plan)

        if lc_files:
            if "Review Required" not in new_plan:
                new_plan["Review Required"] = {}
            new_plan["Review Required"].update(lc_files)

        return new_plan, error

    def __init__(self, model_path: str = None):
        self._generator = None
        self.task = None
        self.token_biases = {}

        from app.core.path_utils import get_base_path

        base_path = get_base_path(__file__)

        local_bundle_path = os.path.join(base_path, "offline_bundle", "model")

        from app.config import get_app_dir

        user_bundle_path = str(get_app_dir() / "model")

        self.model_path = model_path
        if not self.model_path:
            import sys

            if hasattr(sys, "_MEIPASS"):
                mei_bundle_path = os.path.join(sys._MEIPASS, "offline_bundle", "model")
                if os.path.exists(mei_bundle_path):
                    self.model_path = mei_bundle_path

            if not self.model_path:
                if os.path.exists(local_bundle_path):
                    self.model_path = local_bundle_path
                elif os.path.exists(user_bundle_path):
                    self.model_path = user_bundle_path
                else:
                    self.model_path = None

        self._model_initialized = False
        self._gguf_active = False
        self._gguf_failed = False
        self._gguf_process = None
        self._gguf_input_queue = None
        self._gguf_output_queue = None

    @property
    def generator(self):
        """Get or lazily initialize the generative text model generator."""
        if self._generator is not None:
            return self._generator
        from app.core.shared_registry import SharedModelRegistry
        registry = SharedModelRegistry.get_instance()
        if not registry.is_model_loaded("generative_naming"):
            if getattr(self, "_model_initialized", False) and getattr(self, "model_path", None):
                gen, task, tok = registry.get_generative_model(self.model_path)
                self.task = task
                if tok:
                    self.token_biases = self._build_logit_biases(tok)
                self._generator = gen
                return gen
            return None
        gen, task, tok = registry.get_generative_model(self.model_path)
        self.task = task
        if tok:
            self.token_biases = self._build_logit_biases(tok)
        self._generator = gen
        return gen

    @generator.setter
    def generator(self, value):
        self._generator = value

    def unload(self):
        """Unload generative naming strategy model references."""
        self._generator = None
        self._model_initialized = False

    def _init_model(self):
        self._model_initialized = True

        if not self._gguf_failed and is_gguf_model_dir(self.model_path):
            try:
                self._gguf_active = True
                self._gguf_input_queue = multiprocessing.Queue()
                self._gguf_output_queue = multiprocessing.Queue()

                from app.core.shared_registry import SharedModelRegistry

                try:
                    n_threads = SharedModelRegistry.get_instance().get_thread_limit()
                except Exception:
                    n_threads = os.cpu_count() or multiprocessing.cpu_count() or 2

                self._gguf_process = multiprocessing.Process(
                    target=gguf_worker_main,
                    args=(
                        self.model_path,
                        self._gguf_input_queue,
                        self._gguf_output_queue,
                        n_threads,
                    ),
                )
                self._gguf_process.start()

                res = cooperative_queue_get(self._gguf_output_queue, timeout=10.0)
                if not isinstance(res, dict) or "error" in res:
                    raise Exception(
                        res.get("error")
                        if isinstance(res, dict)
                        else "Unknown initialization error"
                    )
                return
            except Exception as e:
                logging.error(f"GGUF initialization failed: {e}")
                self._fallback_to_pytorch()
                return

        self._init_pytorch_model()

    def _fallback_to_pytorch(self):
        self._gguf_failed = True
        self._gguf_active = False
        if self._gguf_process:
            try:
                self._gguf_process.terminate()
                cooperative_join(self._gguf_process, timeout=1.0)
                if self._gguf_process.is_alive():
                    self._gguf_process.kill()
                self._gguf_process.close()
            except Exception:
                pass
            self._gguf_process = None

        from app.core.path_utils import is_packaged

        if is_packaged():
            base_path = os.path.dirname(sys.executable)
        else:
            base_path = os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            )

        local_bundle_path = os.path.join(base_path, "offline_bundle", "model")
        from app.config import get_app_dir

        user_bundle_path = str(get_app_dir() / "model")

        if not self.model_path or not os.path.exists(
            os.path.join(self.model_path, "config.json")
        ):
            if hasattr(sys, "_MEIPASS"):
                mei_bundle_path = os.path.join(sys._MEIPASS, "offline_bundle", "model")
                if os.path.exists(mei_bundle_path):
                    self.model_path = mei_bundle_path

            if not self.model_path or not os.path.exists(
                os.path.join(self.model_path, "config.json")
            ):
                if os.path.exists(local_bundle_path):
                    self.model_path = local_bundle_path
                elif os.path.exists(user_bundle_path):
                    self.model_path = user_bundle_path
                else:
                    self.model_path = None

        self._init_pytorch_model()

    def _init_pytorch_model(self):
        self._model_initialized = True
        if not self.model_path or not os.path.exists(self.model_path):
            logging.warning(
                "Offline model bundle not found in either the local project directory or the user configuration directory."
            )
            return

        from app.core.shared_registry import SharedModelRegistry

        registry = SharedModelRegistry.get_instance()
        try:
            generator, task, tokenizer = registry.get_generative_model(self.model_path)
            self.generator = generator
            self.task = task
            if tokenizer:
                self.token_biases = self._build_logit_biases(tokenizer)
        except Exception as e:
            logging.error(f"Failed to load generative model via shared registry: {e}")
            self.generator = None

    def _run_prompt(self, prompt: str, max_tokens: int, grammar: str = None) -> str:
        if is_prompt_dump_enabled():
            dump_file = os.environ.get("PROMPT_DUMP_FILE")
            try:
                validate_prompt_dump_path(dump_file)
                scrubbed_prompt = scrub_prompt_text(prompt)
                parent_dir = os.path.dirname(dump_file)
                if parent_dir:
                    os.makedirs(parent_dir, exist_ok=True)
                with open(dump_file, "a", encoding="utf-8") as f:
                    f.write(scrubbed_prompt + "\n===PROMPT_END===\n")
                return "Mock Generated Folder Name"
            except Exception as e:
                logging.warning(
                    f"Failed to write prompt dump to '{dump_file}': {e}"
                )

        if self._gguf_active and not self._gguf_failed:
            if not self._gguf_process or not self._gguf_process.is_alive():
                logging.error("GGUF process died unexpectedly")
                self._fallback_to_pytorch()
            else:
                try:
                    self._gguf_input_queue.put(
                        {"prompt": prompt, "max_tokens": max_tokens, "grammar": grammar}
                    )
                    estimated_tokens = len(prompt) // 4
                    timeout = max(8.0, min(60.0, 8.0 + (estimated_tokens / 20.0)))
                    res = cooperative_queue_get(
                        self._gguf_output_queue, timeout=timeout
                    )
                    if not isinstance(res, dict) or "error" in res or "text" not in res:
                        raise Exception(
                            res.get("error")
                            if isinstance(res, dict)
                            else "Null or incomplete response"
                        )
                    return res["text"]
                except Exception as e:
                    logging.error(f"GGUF worker failed: {e}")
                    self._fallback_to_pytorch()

        if self.generator is None:
            return ""

        import torch
        from transformers import LogitsProcessorList

        from app.core.shared_registry import SharedModelRegistry

        torch.set_num_threads(SharedModelRegistry.get_instance().get_thread_limit())

        logits_processor = LogitsProcessorList()
        if getattr(self, "token_biases", None):
            logits_processor.append(NegativeLogitBiasProcessor(self.token_biases))

        if self.task == "text-generation":
            res = self.generator(
                prompt,
                max_new_tokens=max_tokens,
                num_return_sequences=1,
                return_full_text=False,
                logits_processor=logits_processor,
            )
        else:
            res = self.generator(
                prompt,
                max_new_tokens=max_tokens,
                num_return_sequences=1,
                logits_processor=logits_processor,
            )
        return res[0]["generated_text"]

    def _should_bias_token(self, token_str: str) -> bool:
        # Clean token of special tokenizer characters representing spaces or unk
        clean_str = (
            token_str.replace("Ġ", "").replace(" ", "").replace("<unk>", "").strip()
        )
        if not clean_str:
            return False

        # Hyphen and punctuation check
        import string

        if any(c in string.punctuation for c in clean_str):
            return True

        # Conversational filler words
        lower_str = clean_str.lower()
        if lower_str in {
            "sure",
            "here",
            "is",
            "a",
            "an",
            "the",
            "this",
            "these",
            "it",
            "they",
            "them",
            "there",
            "are",
            "of",
            "some",
            "document",
            "documents",
            "file",
            "files",
            "folder",
            "folders",
            "containing",
            "about",
            "for",
            "named",
            "associated",
            "with",
            "relating",
            "to",
            "and",
            "in",
            "at",
            "by",
            "from",
            "or",
            "as",
            "but",
            "so",
            "if",
            "then",
            "else",
            "under",
            "below",
            "above",
            "following",
            "list",
            "items",
            "content",
            "contents",
            "yes",
            "no",
            "ok",
            "okay",
            "hello",
            "hi",
            "hey",
            "please",
            "find",
            "attached",
            "generated",
            "name",
            "names",
            "title",
            "titles",
        }:
            return True

        return False

    def _build_logit_biases(self, tokenizer):
        token_biases = {}
        try:
            vocab = tokenizer.get_vocab()
            for token_str, token_id in vocab.items():
                if self._should_bias_token(token_str):
                    token_biases[token_id] = -100.0
        except Exception:
            try:
                vocab_size = getattr(tokenizer, "vocab_size", None)
                if vocab_size is None:
                    vocab_size = len(tokenizer)
                for token_id in range(vocab_size):
                    token_str = tokenizer.convert_ids_to_tokens(token_id)
                    if isinstance(token_str, str) and self._should_bias_token(
                        token_str
                    ):
                        token_biases[token_id] = -100.0
            except Exception as e:
                logging.error(f"Failed to build logit biases: {e}")
        return token_biases

    def set_db_context(self, db, base_dir):
        """Set the database and base directory context for historical queries."""
        self.db = db
        self.base_dir = base_dir

    def _get_cluster_keywords(self, documents: list) -> str:
        import os
        from collections import defaultdict

        import numpy as np

        if not documents:
            return "Miscellaneous"

        # Coherence routing & Offline Similarity matching layer
        cached_decrypted_db_rows = None
        if getattr(self, "model_path", None):
            db = getattr(self, "db", None)
            base_dir = getattr(self, "base_dir", None)
            embedding_manager = None
            use_semantic = False

            pre_fetched_corpus = getattr(self, "pre_fetched_corpus", None)
            if pre_fetched_corpus is not None:
                model_metadata = pre_fetched_corpus.get("model_metadata")
                if model_metadata and getattr(self, "model_path", None):
                    try:
                        from app.core.semantic_embeddings import (
                            SemanticEmbeddingManager,
                        )

                        class InMemoryDBMock:
                            def __init__(self, meta):
                                self._meta = meta or {}

                            def get_model_metadata(self, key):
                                return self._meta.get(key)

                            def set_model_metadata(self, key, value):
                                self._meta[key] = value

                        dummy_db = InMemoryDBMock(model_metadata)
                        embedding_manager = SemanticEmbeddingManager(
                            dummy_db, model_path=self.model_path
                        )
                        if (
                            not embedding_manager.is_mock
                            and not embedding_manager.is_reconstruction_active()
                        ):
                            use_semantic = True
                    except Exception as e:
                        logging.error(
                            f"Failed to initialize SemanticEmbeddingManager in child process: {e}"
                        )
            elif db:
                try:
                    from app.core.semantic_embeddings import SemanticEmbeddingManager

                    embedding_manager = SemanticEmbeddingManager(
                        db, model_path=getattr(self, "model_path", None)
                    )
                    if (
                        not embedding_manager.is_mock
                        and not embedding_manager.is_reconstruction_active()
                    ):
                        use_semantic = True
                except Exception as e:
                    logging.error(
                        f"Failed to initialize SemanticEmbeddingManager for strategy: {e}"
                    )

            filtered_documents = [
                doc
                for doc in documents
                if doc and not doc.startswith("[STATUS:") and doc.strip()
            ]
            if not filtered_documents:
                filtered_documents = documents

            cluster_vectors = []
            if use_semantic and embedding_manager:
                try:
                    cluster_vectors = [
                        embedding_manager.get_embedding(doc)
                        for doc in filtered_documents
                    ]
                except Exception as e:
                    logging.error(f"Failed to generate embeddings: {e}")
                    cluster_vectors = []

            if not use_semantic or not cluster_vectors:
                try:
                    from sklearn.feature_extraction.text import TfidfVectorizer

                    vectorizer = TfidfVectorizer(
                        stop_words=list(self.stop_words)
                        if getattr(self, "stop_words", None)
                        else "english",
                        max_features=1000,
                    )
                    safe_docs = [doc or "" for doc in filtered_documents]
                    X = vectorizer.fit_transform(safe_docs)
                    cluster_vectors = [row.toarray()[0] for row in X]
                except Exception as e:
                    logging.error(
                        f"Failed to generate TF-IDF vectors for coherence: {e}"
                    )
                    cluster_vectors = []

            if cluster_vectors:
                try:

                    def cosine_sim(v1, v2):
                        v1_arr = np.array(v1)
                        v2_arr = np.array(v2)
                        norm1 = np.linalg.norm(v1_arr)
                        norm2 = np.linalg.norm(v2_arr)
                        if norm1 == 0 or norm2 == 0:
                            return 0.0
                        return float(np.dot(v1_arr, v2_arr) / (norm1 * norm2))

                    # Calculate cluster centroid
                    cluster_centroid = np.mean(cluster_vectors, axis=0)

                    # Calculate cohesion score: average cosine similarity of each doc to the centroid
                    coherences = [
                        cosine_sim(v, cluster_centroid) for v in cluster_vectors
                    ]
                    cohesion_score = np.mean(coherences) if coherences else 1.0

                    # Check cohesion score threshold
                    if cohesion_score < 0.3:
                        logging.info(
                            f"Cohesion score {cohesion_score:.4f} is below 0.3. Routing to 'Review Required' without initiating generative model."
                        )
                        return "Review Required"

                    # Compare against a history of user-verified folder vectors to determine semantic similarity
                    historical_folder_vectors = defaultdict(list)
                    if pre_fetched_corpus is not None:
                        for ex in pre_fetched_corpus.get("examples", []):
                            folder = ex.get("user_verified_target_path")
                            v = ex.get("vector")
                            if use_semantic:
                                if folder and v:
                                    if embedding_manager.validate_vector_dimension(v):
                                        historical_folder_vectors[folder].append(v)
                    elif db and base_dir:
                        try:
                            from app.core.db_conn import get_db_connection

                            conn = get_db_connection(db.db_path)
                            with conn:
                                cursor = conn.execute(
                                    """
                                    SELECT d.filepath, d.user_verified_target_path, v.vector
                                    FROM documents d
                                    JOIN document_vectors v ON d.base_dir = v.base_dir AND d.filepath = v.filepath
                                    WHERE d.base_dir = ? AND d.user_verified_target_path IS NOT NULL AND d.user_verified_target_path != ''
                                    """,
                                    (base_dir,),
                                )
                                rows = cursor.fetchall()

                            def decrypt_chunk(chunk):
                                chunk_results = []
                                for filepath, folder, vector_str in chunk:
                                    if folder and vector_str:
                                        try:
                                            v = db.crypto.decrypt_and_parse_vector(
                                                vector_str
                                            )
                                            chunk_results.append((filepath, folder, v))
                                        except Exception:
                                            chunk_results.append(
                                                (filepath, folder, None)
                                            )
                                    else:
                                        chunk_results.append((None, None, None))
                                return chunk_results

                            if len(rows) <= 32:
                                decrypted_results = decrypt_chunk(rows)
                            else:
                                executor = get_decryption_executor()
                                num_chunks = min(32, max(1, len(rows) // 32))
                                step = (len(rows) + num_chunks - 1) // num_chunks
                                chunks = [
                                    rows[i : i + step]
                                    for i in range(0, len(rows), step)
                                ]
                                chunk_res = list(executor.map(decrypt_chunk, chunks))
                                decrypted_results = [
                                    item for sub in chunk_res for item in sub
                                ]

                            cached_decrypted_db_rows = decrypted_results

                            for filepath, folder, v in decrypted_results:
                                if folder:
                                    if v is not None:
                                        if use_semantic:
                                            if embedding_manager.validate_vector_dimension(
                                                v
                                            ):
                                                historical_folder_vectors[
                                                    folder
                                                ].append(v)
                                    else:
                                        db.track_corrupted_vector(base_dir, filepath)
                        except Exception as e:
                            logging.error(
                                f"Failed to query historical folder vectors: {e}"
                            )

                    # If TF-IDF mode, vectorize historical documents using TF-IDF
                    if not use_semantic:
                        historical_folder_texts = defaultdict(list)
                        if pre_fetched_corpus is not None:
                            for ex in pre_fetched_corpus.get("examples", []):
                                folder = ex.get("user_verified_target_path")
                                text = ex.get("text")
                                if folder and text:
                                    historical_folder_texts[folder].append(text)
                        elif db and base_dir:
                            try:
                                all_docs = db.get_all_documents(base_dir)
                                for doc in all_docs:
                                    if len(doc) > 3 and doc[1] and doc[3]:
                                        folder = doc[3]
                                        text = doc[1]
                                        if (
                                            folder
                                            and text
                                            and not text.startswith("[STATUS:")
                                        ):
                                            historical_folder_texts[folder].append(text)
                            except Exception as e:
                                logging.error(
                                    f"Failed to query historical docs for TF-IDF: {e}"
                                )

                        if historical_folder_texts:
                            try:
                                all_texts = []
                                folder_indices = []
                                for folder, texts in historical_folder_texts.items():
                                    for text in texts:
                                        all_texts.append(text)
                                        folder_indices.append(folder)

                                if all_texts:
                                    from sklearn.feature_extraction.text import (
                                        TfidfVectorizer,
                                    )

                                    hist_vectorizer = TfidfVectorizer(
                                        stop_words=list(self.stop_words)
                                        if getattr(self, "stop_words", None)
                                        else "english",
                                        max_features=1000,
                                    )
                                    hist_X = hist_vectorizer.fit_transform(all_texts)
                                    curr_X = hist_vectorizer.transform(
                                        filtered_documents
                                    )
                                    cluster_vectors_tfidf = [
                                        row.toarray()[0] for row in curr_X
                                    ]
                                    cluster_centroid_tfidf = np.mean(
                                        cluster_vectors_tfidf, axis=0
                                    )

                                    for idx, folder in enumerate(folder_indices):
                                        vec = hist_X[idx].toarray()[0]
                                        historical_folder_vectors[folder].append(vec)

                                    cluster_centroid = cluster_centroid_tfidf
                            except Exception as e:
                                logging.error(
                                    f"Failed to compute historical TF-IDF vectors: {e}"
                                )

                    # Calculate historical folder centroids and best match similarity
                    historical_folder_centroids = {}
                    for folder, vecs in historical_folder_vectors.items():
                        if vecs:
                            historical_folder_centroids[folder] = np.asarray(
                                vecs, dtype=np.float32
                            ).mean(axis=0)

                    best_match_folder = None
                    best_match_similarity = -1.0

                    if historical_folder_centroids:
                        folders = list(historical_folder_centroids.keys())
                        centroids_matrix = np.array(
                            [historical_folder_centroids[f] for f in folders],
                            dtype=np.float32,
                        )
                        centroids_norms = np.linalg.norm(centroids_matrix, axis=1)
                        c_norm = float(np.linalg.norm(cluster_centroid))

                        if c_norm > 0:
                            dot_products = centroids_matrix @ np.asarray(
                                cluster_centroid, dtype=np.float32
                            )
                            denom = centroids_norms * c_norm
                            denom = np.where(denom == 0, 1.0, denom)
                            sims = dot_products / denom
                            sims = np.where(centroids_norms == 0, 0.0, sims)

                            best_idx = int(np.argmax(sims))
                            best_match_similarity = float(sims[best_idx])
                            best_match_folder = folders[best_idx]

                    # Apply thresholds to routing
                    if historical_folder_centroids:
                        if best_match_similarity >= 0.85:
                            logging.info(
                                f"High-confidence match: {best_match_folder} (similarity {best_match_similarity:.4f} >= 0.85). Bypassing generative."
                            )
                            return best_match_folder

                        if best_match_similarity < 0.3:
                            logging.info(
                                f"Similarity match {best_match_similarity:.4f} is below 0.3. Bypassing generative and fallback to TF-IDF naming."
                            )
                            return super()._get_cluster_keywords(documents)

                        logging.info(
                            f"Similarity match {best_match_similarity:.4f} falls between 0.3 and 0.85. Proceeding with generative model naming."
                        )
                    else:
                        logging.info("Proceeding with generative model naming.")
                except Exception as e:
                    logging.error(
                        f"Error in coherence/similarity routing layer: {e}",
                        exc_info=True,
                    )

        if not getattr(self, "_model_initialized", False):
            self._init_model()

        if (
            self.generator is None
            and not (self._gguf_active and not self._gguf_failed)
            and not is_prompt_dump_enabled()
        ):
            return super()._get_cluster_keywords(documents)

        db = getattr(self, "db", None)
        base_dir = getattr(self, "base_dir", None)
        use_semantic = False
        top_examples = []
        few_shot_context = ""
        # Filter out non-textual attachments and skipped/unsupported files from target documents
        filtered_documents = [
            doc
            for doc in documents
            if doc and not doc.startswith("[STATUS:") and doc.strip()
        ]
        if not filtered_documents:
            filtered_documents = documents

        pre_fetched_corpus = getattr(self, "pre_fetched_corpus", None)
        if pre_fetched_corpus is not None:
            # In-memory pre-fetched few-shot logic
            model_metadata = pre_fetched_corpus.get("model_metadata")
            if model_metadata and getattr(self, "model_path", None):
                try:
                    from app.core.semantic_embeddings import SemanticEmbeddingManager

                    class InMemoryDBMock:
                        def __init__(self, meta):
                            self._meta = meta or {}

                        def get_model_metadata(self, key):
                            return self._meta.get(key)

                        def set_model_metadata(self, key, value):
                            self._meta[key] = value

                    dummy_db = InMemoryDBMock(model_metadata)
                    embedding_manager = SemanticEmbeddingManager(
                        dummy_db, model_path=self.model_path
                    )
                    if (
                        not embedding_manager.is_mock
                        and not embedding_manager.is_reconstruction_active()
                    ):
                        use_semantic = True
                except Exception as e:
                    logging.error(
                        f"Failed to initialize SemanticEmbeddingManager in child process: {e}"
                    )
                    use_semantic = False

                if use_semantic:
                    try:
                        target_text = " ".join(filtered_documents)
                        target_vector = embedding_manager.get_embedding(target_text)
                        if (
                            target_vector
                            and embedding_manager.validate_vector_dimension(
                                target_vector
                            )
                        ):
                            hist_vectors = []
                            hist_meta = []
                            supported_exts_set = {
                                ".txt",
                                ".docx",
                                ".csv",
                                ".xlsx",
                                ".xls",
                                ".pdf",
                            }
                            for ex in pre_fetched_corpus.get("examples", []):
                                filepath = ex["filepath"]
                                user_verified_target = ex["user_verified_target_path"]
                                v = ex.get("vector")
                                if v:
                                    dot_idx = filepath.rfind(".")
                                    ext = (
                                        filepath[dot_idx:].lower()
                                        if dot_idx != -1
                                        else ""
                                    )
                                    if (
                                        ext in {".png", ".jpg", ".jpeg"}
                                        or ext not in supported_exts_set
                                    ):
                                        continue
                                    if embedding_manager.validate_vector_dimension(v):
                                        hist_vectors.append(v)
                                        hist_meta.append(
                                            {
                                                "filepath": filepath,
                                                "user_verified_target_path": user_verified_target,
                                                "text": ex.get("text", ""),
                                            }
                                        )

                            if hist_vectors:
                                import numpy as np
                                from sklearn.metrics.pairwise import cosine_similarity

                                target_vector_arr = np.array([target_vector])
                                hist_vectors_arr = np.array(hist_vectors)
                                similarities = cosine_similarity(
                                    target_vector_arr, hist_vectors_arr
                                ).flatten()

                                sorted_indices = similarities.argsort()[::-1]

                                for idx in sorted_indices:
                                    if similarities[idx] >= 0.1:
                                        top_examples.append(
                                            (hist_meta[idx], similarities[idx])
                                        )
                                        if len(top_examples) >= 3:
                                            break

                                if top_examples:
                                    few_shot_lines = []
                                    few_shot_lines.append(
                                        "Here are some historical examples of documents and their corresponding user-corrected folder names:"
                                    )
                                    for ex_idx, (ex, sim) in enumerate(top_examples):
                                        snippet = (
                                            ex.get("text", "")[:500]
                                            .replace("\n", " ")
                                            .strip()
                                        )
                                        if not snippet:
                                            snippet = os.path.basename(ex["filepath"])

                                        folder_name = ex["user_verified_target_path"]
                                        few_shot_lines.append(
                                            f"Example {ex_idx + 1}:\nDocument: {snippet}\nFolder Name: {folder_name}"
                                        )
                                    few_shot_context = (
                                        "\n\n".join(few_shot_lines) + "\n\n"
                                    )
                    except Exception as e:
                        logging.error(
                            f"Semantic historical matching failed in child process: {e}"
                        )
                        top_examples = []
                        few_shot_context = ""

            if not use_semantic or not top_examples:
                fallback_top_examples = []
                historical_examples = []
                for ex in pre_fetched_corpus.get("examples", []):
                    filepath = ex["filepath"]
                    if not filepath.lower().endswith(
                        (".txt", ".docx", ".csv", ".xlsx", ".xls", ".pdf")
                    ):
                        continue
                    decrypted_text = ex.get("text", "")
                    if decrypted_text.startswith("[STATUS:"):
                        continue

                    historical_examples.append(
                        {
                            "text": decrypted_text,
                            "target_path": ex["user_verified_target_path"],
                        }
                    )

                if historical_examples:
                    try:
                        from sklearn.feature_extraction.text import TfidfVectorizer
                        from sklearn.metrics.pairwise import cosine_similarity

                        stop_words_list = (
                            list(self.stop_words)
                            if getattr(self, "stop_words", None)
                            else "english"
                        )
                        vectorizer = TfidfVectorizer(
                            stop_words=stop_words_list,
                            max_features=1000,
                            sublinear_tf=True,
                        )

                        hist_texts = [ex["text"] for ex in historical_examples]
                        target_text = " ".join(filtered_documents)

                        hist_vectors = vectorizer.fit_transform(hist_texts)
                        target_vector = vectorizer.transform([target_text])

                        similarities = cosine_similarity(
                            target_vector, hist_vectors
                        ).flatten()

                        sorted_indices = similarities.argsort()[::-1]

                        for idx in sorted_indices:
                            if similarities[idx] >= 0.1:
                                fallback_top_examples.append(
                                    (historical_examples[idx], similarities[idx])
                                )
                                if len(fallback_top_examples) >= 3:
                                    break
                    except Exception as e:
                        logging.error(
                            f"Error querying TF-IDF historical examples in child process fallback: {e}"
                        )

                if fallback_top_examples:
                    try:
                        few_shot_lines = []
                        few_shot_lines.append(
                            "Here are some historical examples of documents and their corresponding user-corrected folder names:"
                        )
                        for ex_idx, (ex, sim) in enumerate(fallback_top_examples):
                            snippet = ex["text"][:500].replace("\n", " ").strip()
                            folder_name = ex["target_path"]
                            few_shot_lines.append(
                                f"Example {ex_idx + 1}:\nDocument: {snippet}\nFolder Name: {folder_name}"
                            )
                        few_shot_context = "\n\n".join(few_shot_lines) + "\n\n"
                    except Exception as e:
                        logging.error(
                            f"Error formatting few_shot_context in child process fallback: {e}"
                        )

        elif db and base_dir:
            try:
                from app.core.semantic_embeddings import SemanticEmbeddingManager

                embedding_manager = SemanticEmbeddingManager(
                    db, model_path=getattr(self, "model_path", None)
                )
                if (
                    not embedding_manager.is_mock
                    and not embedding_manager.is_reconstruction_active()
                ):
                    use_semantic = True
            except Exception as e:
                logging.error(
                    f"Failed to initialize SemanticEmbeddingManager for strategy: {e}"
                )
                use_semantic = False

            if use_semantic:
                try:
                    # 1. Compute Cluster Query Vector using full text to avoid truncation artifacts
                    target_text = " ".join(filtered_documents)
                    target_vector = embedding_manager.get_embedding(target_text)
                    if target_vector and embedding_manager.validate_vector_dimension(
                        target_vector
                    ):
                        hist_vectors = []
                        hist_meta = []
                        supported_exts_set = {
                            ".txt",
                            ".docx",
                            ".csv",
                            ".xlsx",
                            ".xls",
                            ".pdf",
                        }

                        if cached_decrypted_db_rows is not None:
                            decrypted_hist_results = cached_decrypted_db_rows
                        else:
                            # 2. Query Pre-computed Historical Vectors directly from DB
                            from app.core.db_conn import get_db_connection

                            conn = get_db_connection(db.db_path)
                            with conn:
                                cursor = conn.execute(
                                    """
                                    SELECT d.filepath, d.user_verified_target_path, v.vector
                                    FROM documents d
                                    JOIN document_vectors v ON d.base_dir = v.base_dir AND d.filepath = v.filepath
                                    WHERE d.base_dir = ? AND d.user_verified_target_path IS NOT NULL AND d.user_verified_target_path != ''
                                """,
                                    (base_dir,),
                                )
                                rows = cursor.fetchall()

                            if rows:

                                def decrypt_historical_item(row):
                                    filepath, user_verified_target, vector_str = row
                                    dot_idx = filepath.rfind(".")
                                    ext = (
                                        filepath[dot_idx:].lower()
                                        if dot_idx != -1
                                        else ""
                                    )
                                    # Exclude non-textual attachments and image files from semantic similarity
                                    if (
                                        ext in {".png", ".jpg", ".jpeg"}
                                        or ext not in supported_exts_set
                                    ):
                                        return None, None, None

                                    if vector_str:
                                        try:
                                            v = db.crypto.decrypt_and_parse_vector(
                                                vector_str
                                            )
                                            return filepath, user_verified_target, v
                                        except Exception:
                                            return filepath, user_verified_target, None
                                    return None, None, None

                                executor = get_decryption_executor()
                                decrypted_hist_results = list(
                                    executor.map(decrypt_historical_item, rows)
                                )
                            else:
                                decrypted_hist_results = []

                        if decrypted_hist_results:
                            import numpy as np
                            from sklearn.metrics.pairwise import cosine_similarity

                            for (
                                filepath,
                                user_verified_target,
                                v,
                            ) in decrypted_hist_results:
                                if filepath:
                                    dot_idx = filepath.rfind(".")
                                    ext = (
                                        filepath[dot_idx:].lower()
                                        if dot_idx != -1
                                        else ""
                                    )
                                    if (
                                        ext in {".png", ".jpg", ".jpeg"}
                                        or ext not in supported_exts_set
                                    ):
                                        continue
                                    if v is not None:
                                        if embedding_manager.validate_vector_dimension(
                                            v
                                        ):
                                            hist_vectors.append(v)
                                            hist_meta.append(
                                                {
                                                    "filepath": filepath,
                                                    "user_verified_target_path": user_verified_target,
                                                }
                                            )
                                    else:
                                        db.track_corrupted_vector(base_dir, filepath)

                            if hist_vectors:
                                # 3. Cosine Similarity Calculation
                                hist_vectors_arr = np.asarray(
                                    hist_vectors, dtype=np.float32
                                )
                                target_vector_arr = np.asarray(
                                    target_vector, dtype=np.float32
                                )
                                target_norm = np.linalg.norm(target_vector_arr)
                                hist_norms = np.linalg.norm(hist_vectors_arr, axis=1)
                                denom = hist_norms * target_norm
                                denom[denom == 0] = 1e-9
                                similarities = (
                                    hist_vectors_arr @ target_vector_arr
                                ) / denom

                                sorted_indices = similarities.argsort()[::-1]

                                for idx in sorted_indices:
                                    if similarities[idx] >= 0.1:
                                        top_examples.append(
                                            (hist_meta[idx], float(similarities[idx]))
                                        )
                                        if len(top_examples) >= 3:
                                            break

                                if top_examples:
                                    few_shot_lines = []
                                    few_shot_lines.append(
                                        "Here are some historical examples of documents and their corresponding user-corrected folder names:"
                                    )
                                    # Fetch top exemplar texts efficiently in batch
                                    doc_snippets = {}
                                    if db and base_dir and top_examples:
                                        try:
                                            target_fps = [
                                                ex["filepath"].replace("\\", "/")
                                                for ex, _ in top_examples
                                            ]
                                            # Check cache first
                                            with db._cache_lock:
                                                if (
                                                    db._cached_base_dir == base_dir
                                                    and db._cached_documents is not None
                                                ):
                                                    for row in db._cached_documents:
                                                        if row[0] in target_fps:
                                                            doc_snippets[row[0]] = row[
                                                                1
                                                            ]

                                            # Fetch any remaining from DB in a single query
                                            missing_fps = [
                                                fp
                                                for fp in target_fps
                                                if fp not in doc_snippets
                                            ]
                                            if missing_fps:
                                                from app.core.db_conn import (
                                                    get_db_connection,
                                                )

                                                conn = get_db_connection(db.db_path)
                                                with conn:
                                                    placeholders = ",".join(
                                                        ["?"] * len(missing_fps)
                                                    )
                                                    cursor = conn.execute(
                                                        f"SELECT filepath, extracted_text FROM documents WHERE base_dir = ? AND filepath IN ({placeholders})",
                                                        [base_dir] + missing_fps,
                                                    )
                                                    for row in cursor.fetchall():
                                                        raw_text = row[1]
                                                        if raw_text is not None:
                                                            try:
                                                                decrypted = db.crypto.decrypt_text(
                                                                    raw_text
                                                                )
                                                            except Exception:
                                                                decrypted = raw_text
                                                            doc_snippets[row[0]] = (
                                                                decrypted
                                                            )
                                        except Exception as e:
                                            logging.error(
                                                f"Failed to fetch decrypted document snippets for semantic exemplars: {e}"
                                            )

                                    for ex_idx, (ex, sim) in enumerate(top_examples):
                                        import os

                                        fp = ex["filepath"].replace("\\", "/")
                                        raw_snippet = doc_snippets.get(fp)
                                        snippet = (
                                            raw_snippet[:500].replace("\n", " ").strip()
                                            if raw_snippet
                                            else None
                                        )
                                        if not snippet:
                                            snippet = os.path.basename(ex["filepath"])

                                        folder_name = ex["user_verified_target_path"]
                                        few_shot_lines.append(
                                            f"Example {ex_idx + 1}:\nDocument: {snippet}\nFolder Name: {folder_name}"
                                        )
                                    few_shot_context = (
                                        "\n\n".join(few_shot_lines) + "\n\n"
                                    )
                except Exception as e:
                    logging.error(f"Semantic historical matching failed: {e}")
                    top_examples = []
                    few_shot_context = ""

        if (not pre_fetched_corpus) and (not use_semantic or not top_examples):
            # Fallback path (Keyword-Based Matching)
            few_shot_context = ""
            fallback_top_examples = []

            if db and base_dir:
                try:
                    import math

                    import numpy as np

                    # 1. Retrieve the incremental TF-IDF statistics from DB
                    N, top_terms, doc_terms, doc_metadata = db.get_tfidf_stats(base_dir)

                    if N > 0 and top_terms and doc_terms:
                        # Build vocabulary mapping and IDF weights
                        vocab = {term: idx for idx, (term, df) in enumerate(top_terms)}
                        idf_weights = {
                            term: math.log((1 + N) / (1 + df)) + 1
                            for term, df in top_terms
                        }

                        # Group doc_terms by filepath
                        from collections import defaultdict

                        doc_tfs = defaultdict(list)
                        for filepath, term, tf in doc_terms:
                            doc_tfs[filepath].append((term, tf))

                        # Compute document vectors for documents that have metadata
                        hist_vectors = []
                        historical_examples_meta = []

                        for filepath, target_path in doc_metadata.items():
                            tfs = doc_tfs.get(filepath)
                            if not tfs:
                                continue
                            vec = np.zeros(len(vocab))
                            for term, tf in tfs:
                                if term in vocab:
                                    idx = vocab[term]
                                    tf_weight = 1 + math.log(tf)
                                    vec[idx] = tf_weight * idf_weights[term]
                            norm = np.linalg.norm(vec)
                            if norm > 0:
                                vec = vec / norm
                            hist_vectors.append(vec)
                            historical_examples_meta.append(
                                {"filepath": filepath, "target_path": target_path}
                            )

                        if hist_vectors:
                            # Tokenize target text
                            target_text = " ".join(filtered_documents)
                            stop_words_list = (
                                list(self.stop_words)
                                if getattr(self, "stop_words", None)
                                else "english"
                            )

                            from collections import Counter

                            from sklearn.feature_extraction.text import TfidfVectorizer

                            try:
                                vectorizer = TfidfVectorizer(stop_words=stop_words_list)
                                analyzer = vectorizer.build_analyzer()
                                target_tokens = analyzer(target_text)
                            except Exception:
                                import re

                                target_tokens = re.findall(
                                    r"\b\w\w+\b", target_text.lower()
                                )
                                from sklearn.feature_extraction import (
                                    text as sklearn_text,
                                )

                                stops = set(sklearn_text.ENGLISH_STOP_WORDS)
                                if (
                                    isinstance(stop_words_list, str)
                                    and stop_words_list == "english"
                                ):
                                    target_tokens = [
                                        t for t in target_tokens if t not in stops
                                    ]
                                elif stop_words_list:
                                    target_tokens = [
                                        t
                                        for t in target_tokens
                                        if t not in stop_words_list
                                    ]

                            target_tfs = Counter(target_tokens)

                            # Build target vector
                            target_vec = np.zeros(len(vocab))
                            for term, tf in target_tfs.items():
                                if term in vocab:
                                    idx = vocab[term]
                                    tf_weight = 1 + math.log(tf)
                                    target_vec[idx] = tf_weight * idf_weights[term]
                            target_norm = np.linalg.norm(target_vec)
                            if target_norm > 0:
                                target_vec = target_vec / target_norm

                            # Calculate cosine similarity using NumPy
                            hist_vectors_arr = np.array(hist_vectors)
                            similarities = np.dot(hist_vectors_arr, target_vec)

                            # Get indices sorted by similarity descending
                            sorted_indices = similarities.argsort()[::-1]

                            # Retrieve top matching examples (up to 3) with similarity >= 0.1
                            for idx in sorted_indices:
                                if similarities[idx] >= 0.1:
                                    filepath = historical_examples_meta[idx]["filepath"]
                                    target_path = historical_examples_meta[idx][
                                        "target_path"
                                    ]

                                    # Fetch decrypted text of only this matching document!
                                    doc_info = db.get_document(base_dir, filepath)
                                    if doc_info and doc_info.get("extracted_text"):
                                        text = doc_info["extracted_text"]
                                        fallback_top_examples.append(
                                            (
                                                {
                                                    "text": text,
                                                    "target_path": target_path,
                                                },
                                                similarities[idx],
                                            )
                                        )
                                    if len(fallback_top_examples) >= 3:
                                        break
                except Exception as e:
                    logging.error(
                        f"Incremental TF-IDF similarity calculation failed: {e}. Falling back to standard."
                    )

            # Traditional fallback if incremental didn't return any top examples
            if not fallback_top_examples:
                historical_examples = []
                if db and base_dir:
                    try:
                        import os

                        all_docs = db.get_all_documents(base_dir)
                        for doc in all_docs:
                            # doc is (filepath, decrypted_text, file_hash, user_verified_target_path)
                            if len(doc) > 3 and doc[1] and doc[3]:
                                filepath = doc[0]
                                if not filepath.lower().endswith(
                                    (".txt", ".docx", ".csv", ".xlsx", ".xls", ".pdf")
                                ):
                                    continue
                                decrypted_text = doc[1]
                                if decrypted_text.startswith("[STATUS:"):
                                    continue

                                historical_examples.append(
                                    {"text": decrypted_text, "target_path": doc[3]}
                                )
                    except Exception as e:
                        logging.error(
                            f"Error reading historical documents from DB for fallback: {e}"
                        )

                if historical_examples:
                    try:
                        from sklearn.feature_extraction.text import TfidfVectorizer
                        from sklearn.metrics.pairwise import cosine_similarity

                        # Limit vocabulary features to 1,000 to keep CPU search speeds fast and minimize latency
                        stop_words_list = (
                            list(self.stop_words)
                            if getattr(self, "stop_words", None)
                            else "english"
                        )
                        # Apply sublinear (logarithmic) term-frequency scaling to dampen highly repetitive terms
                        vectorizer = TfidfVectorizer(
                            stop_words=stop_words_list,
                            max_features=1000,
                            sublinear_tf=True,
                        )

                        hist_texts = [ex["text"] for ex in historical_examples]
                        target_text = " ".join(filtered_documents)

                        # Fit vocabulary and IDF weights exclusively using historical document data to prevent target-driven weight warping
                        hist_vectors = vectorizer.fit_transform(hist_texts)
                        target_vector = vectorizer.transform([target_text])

                        similarities = cosine_similarity(
                            target_vector, hist_vectors
                        ).flatten()

                        # Get indices sorted by similarity descending
                        sorted_indices = similarities.argsort()[::-1]

                        # Retrieve top matching examples (up to 3) with similarity >= 0.1
                        for idx in sorted_indices:
                            if similarities[idx] >= 0.1:
                                fallback_top_examples.append(
                                    (historical_examples[idx], similarities[idx])
                                )
                                if len(fallback_top_examples) >= 3:
                                    break
                    except Exception as e:
                        logging.error(
                            f"Error querying TF-IDF historical examples in fallback: {e}"
                        )

            if fallback_top_examples:
                try:
                    few_shot_lines = []
                    few_shot_lines.append(
                        "Here are some historical examples of documents and their corresponding user-corrected folder names:"
                    )
                    for ex_idx, (ex, sim) in enumerate(fallback_top_examples):
                        # Language model prompt context safety: truncate to 500 characters
                        snippet = ex["text"][:500].replace("\n", " ").strip()
                        folder_name = ex["target_path"]
                        few_shot_lines.append(
                            f"Example {ex_idx + 1}:\nDocument: {snippet}\nFolder Name: {folder_name}"
                        )
                    few_shot_context = "\n\n".join(few_shot_lines) + "\n\n"
                except Exception as e:
                    logging.error(f"Error formatting few_shot_context: {e}")

        try:
            doc_text = " ".join(filtered_documents)[:1000]
            if few_shot_context:
                prompt = (
                    f"{few_shot_context}"
                    f"Now, generate a short, descriptive natural language folder name (1 to 4 words) "
                    f"for a folder containing these documents. Do not use hyphens. Return only the name.\n"
                    f"Documents: {doc_text}\n"
                    f"Folder Name:"
                )
            else:
                prompt = f"Generate a short, descriptive natural language folder name (1 to 4 words) for a folder containing these documents. Do not use hyphens. Return only the name.\nDocuments: {doc_text}\nFolder Name:"

            # Dynamic GBNF Grammar Generation
            try:
                from app.config import AppSettings

                settings = AppSettings()
                ocr_langs = getattr(settings, "OCR_LANGUAGES", "en")

                lang_codes = [
                    lang.strip().lower()
                    for lang in ocr_langs.split(",")
                    if lang.strip()
                ]
                if not lang_codes:
                    lang_codes = ["en"]

                has_en_ranges = False
                has_cjk = False
                has_kana = False
                has_hiragana = False
                has_hangul = False
                other_chars = set()

                for lang in lang_codes:
                    if lang not in LANGUAGE_CHAR_MAP:
                        raise ValueError(
                            f"Unsupported OCR language for grammar constraint: {lang}"
                        )

                    chars_str = LANGUAGE_CHAR_MAP[lang]
                    if "a-z" in chars_str or "A-Z" in chars_str or "0-9" in chars_str:
                        has_en_ranges = True
                    if "\u4e00-\u9fff" in chars_str:
                        has_cjk = True
                    if "\u3040-\u309f" in chars_str:
                        has_hiragana = True
                    if "\u30a0-\u30ff" in chars_str:
                        has_kana = True
                    if "\uac00-\ud7af" in chars_str:
                        has_hangul = True

                    cleaned = (
                        chars_str.replace("a-z", "")
                        .replace("A-Z", "")
                        .replace("0-9", "")
                    )
                    cleaned = (
                        cleaned.replace("\u4e00-\u9fff", "")
                        .replace("\u3040-\u309f", "")
                        .replace("\u30a0-\u30ff", "")
                        .replace("\uac00-\ud7af", "")
                    )
                    for char in cleaned:
                        other_chars.add(char)

                parts = []
                if has_en_ranges:
                    parts.append("a-zA-Z0-9")
                if has_hiragana:
                    parts.append("\u3040-\u309f")
                if has_kana:
                    parts.append("\u30a0-\u30ff")
                if has_hangul:
                    parts.append("\uac00-\ud7af")
                if has_cjk:
                    parts.append("\u4e00-\u9fff")

                sorted_others = "".join(sorted(list(other_chars)))
                parts.append(sorted_others)

                combined_chars = "".join(parts)
                if not combined_chars:
                    combined_chars = "a-zA-Z0-9"

                naming_grammar = f'root ::= word (" " word)? (" " word)? (" " word)?\nword ::= [{combined_chars}]+'
            except Exception as e:
                logging.error(
                    f"Failed to generate dynamic GBNF grammar, falling back to English ASCII: {e}"
                )
                naming_grammar = 'root ::= word (" " word)? (" " word)? (" " word)?\nword ::= [a-zA-Z0-9]+'

            with block_external_network():
                name = self._run_prompt(prompt, 15, grammar=naming_grammar).strip()

                # Cleanup the generated name
                name = name.replace('"', "").replace("-", " ").strip()

                # Replace duplicate whitespace
                name = " ".join(name.split())

                # Limit generated folder name to 1 to 4 words
                words = name.split()
                if len(words) > 4:
                    name = " ".join(words[:4])

                # Strip leading/trailing punctuation
                import string

                name = name.strip(string.punctuation).strip()

                if not name or len(name) < 2:
                    return super()._get_cluster_keywords(documents)

                # Final OS-level path sanitization
                from app.core.path_utils import sanitize_name

                name = sanitize_name(name)

                if not name or len(name) < 2:
                    return super()._get_cluster_keywords(documents)

                return name
        except Exception as e:
            logging.error(f"Generative naming failed: {e}")
            return super()._get_cluster_keywords(documents)


class ClusteringRegistry:
    """Registry for managing and resolving clustering strategies by name."""

    def __init__(self):
        """Initialize the clustering registry with an empty strategy map."""
        self._strategies = {}

    def register(self, name: str, strategy: ClusteringStrategy):
        """Register a new clustering strategy under the given name."""
        self._strategies[name] = strategy

    def get_strategy(self, name: str) -> ClusteringStrategy:
        """Retrieve a clustering strategy by name."""
        if name not in self._strategies:
            if name == "clinical_tmf":
                from app.core.clinical_strategy import ClinicalTMFStrategy

                self._strategies["clinical_tmf"] = ClinicalTMFStrategy(mode="tmf")
            elif name == "clinical_isf":
                from app.core.clinical_strategy import ClinicalTMFStrategy

                self._strategies["clinical_isf"] = ClinicalTMFStrategy(mode="isf")
        return self._strategies.get(name)


clustering_registry = ClusteringRegistry()
clustering_registry.register("default", RecursiveKMeansStrategy())
clustering_registry.register("generative", GenerativeNamingStrategy())
