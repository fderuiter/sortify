"""Defines clustering strategies for grouping documents."""

import logging
import multiprocessing
import os
import sys
from collections import defaultdict
from contextlib import contextmanager
from typing import List, Protocol

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
        cancel_check: callable = None,
    ) -> tuple[dict, float]:
        """Return the clustering plan and the total reconstruction error."""
        ...


class RecursiveKMeansStrategy:
    """Strategy that uses recursive KMeans to cluster documents."""

    def generate_plan(
        self,
        filenames: List[str],
        documents: List[str],
        max_folders: int,
        stop_words: set,
        max_depth: int = 5,
        max_features: int = 3,
        pre_fetched_vectors: List[list] | None = None,
        cancel_check: callable = None,
    ) -> tuple[dict, float]:
        """Return a hierarchical clustering plan and error using KMeans, offloaded to an isolated child process."""
        self.stop_words = stop_words
        self.max_folders = max_folders
        self.max_depth = max_depth
        self.max_features = max_features
        self._error = 0.0

        if pre_fetched_vectors is not None:
            self._vector_map = {f: v for f, v in zip(filenames, pre_fetched_vectors)}
        else:
            self._vector_map = {}

        # Check if we should run in-thread (e.g. during pytest to preserve mock/patch expectations)
        if "PYTEST_CURRENT_TEST" in os.environ and "FORCE_MULTIPROCESSING_TEST" not in os.environ:
            plan = self._cluster_recursive(filenames, documents, depth=1)
            return plan, self._error

        try:
            from app.core.shared_registry import SharedModelRegistry
            thread_limit = SharedModelRegistry.get_instance().get_thread_limit()
        except Exception:
            thread_limit = os.cpu_count() or multiprocessing.cpu_count() or 2

        try:
            ctx = multiprocessing.get_context("spawn")
            output_queue = ctx.Queue()
            p = ctx.Process(
                target=clustering_worker_entry,
                args=(
                    filenames,
                    documents,
                    max_folders,
                    stop_words,
                    max_depth,
                    max_features,
                    pre_fetched_vectors,
                    thread_limit,
                    output_queue,
                ),
            )
            p.start()

            result = None
            import queue
            import time

            try:
                while p.is_alive():
                    if cancel_check and cancel_check():
                        logging.warning("Clustering cancelled by user.")
                        raise RuntimeError("Cancelled")

                    try:
                        result = output_queue.get(timeout=0.05)
                        break
                    except queue.Empty:
                        pass

                    time.sleep(0.01)

                if result is None:
                    try:
                        result = output_queue.get_nowait()
                    except queue.Empty:
                        pass

            except Exception as exc:
                logging.warning(f"Terminating clustering child process due to cancel/exception: {exc}")
                if p.is_alive():
                    p.terminate()
                    cooperative_join(p, timeout=1.0)
                    if p.is_alive():
                        p.kill()
                        cooperative_join(p, timeout=1.0)
                if str(exc) == "Cancelled":
                    return {}, 0.0
                raise

            finally:
                if p.is_alive():
                    p.terminate()
                    cooperative_join(p, timeout=1.0)
                    if p.is_alive():
                        p.kill()
                        cooperative_join(p, timeout=1.0)
                try:
                    p.close()
                except Exception:
                    pass

            if result is None:
                raise RuntimeError("Clustering subprocess died or did not return any result")

            if result.get("status") == "error":
                raise RuntimeError(f"Clustering worker error: {result.get('error')}")

            self._error = result.get("error", 0.0)
            return result.get("plan", {}), self._error

        except Exception as e:
            if str(e) == "Cancelled":
                return {}, 0.0
            logging.error(f"Isolated clustering subprocess failed ({e}). Falling back to local in-thread calculation.")
            plan = self._cluster_recursive(filenames, documents, depth=1)
            return plan, self._error

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
                plan[f] = None
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
                    plan[f] = None
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
                    plan[folder_name][f] = None
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


class _InProcessRecursiveKMeansStrategy(RecursiveKMeansStrategy):
    """Mathematical recursive KMeans executor running inside the isolated child process."""

    def __init__(
        self,
        stop_words: set,
        max_folders: int,
        max_depth: int,
        max_features: int,
        pre_fetched_vectors: List[list] | None,
        filenames: List[str],
    ):
        self.stop_words = stop_words
        self.max_folders = max_folders
        self.max_depth = max_depth
        self.max_features = max_features
        self._error = 0.0

        if pre_fetched_vectors is not None:
            self._vector_map = {f: v for f, v in zip(filenames, pre_fetched_vectors)}
        else:
            self._vector_map = {}


def limit_child_threads(thread_limit: int):
    """Limit CPU multithreading in mathematical/CPU libraries inside the child process."""
    limit_str = str(thread_limit)
    os.environ["OMP_NUM_THREADS"] = limit_str
    os.environ["MKL_NUM_THREADS"] = limit_str
    os.environ["OPENBLAS_NUM_THREADS"] = limit_str
    os.environ["VECLIB_MAXIMUM_THREADS"] = limit_str
    os.environ["NUMEXPR_NUM_THREADS"] = limit_str


def set_child_process_low_priority():
    """Set the current child process to run at low priority (low niceness)."""
    if "CI" in os.environ or "PYTEST_CURRENT_TEST" in os.environ:
        return
    try:
        if sys.platform != "win32":
            os.nice(10)
        else:
            try:
                import psutil
                p = psutil.Process()
                p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
            except Exception:
                import ctypes
                handle = ctypes.windll.kernel32.GetCurrentProcess()
                ctypes.windll.kernel32.SetPriorityClass(handle, 0x00004000)
    except Exception:
        pass


def clustering_worker_entry(
    filenames,
    documents,
    max_folders,
    stop_words,
    max_depth,
    max_features,
    pre_fetched_vectors,
    thread_limit,
    output_queue,
):
    """Entry point for the isolated child process performing core clustering and keyword extraction."""
    try:
        # Set environment CPU thread limits BEFORE loading sklearn/numpy
        limit_child_threads(thread_limit)

        # Set low priority
        set_child_process_low_priority()

        # Set torch thread limits if torch is in sys.modules
        if "torch" in sys.modules:
            try:
                import torch
                torch.set_num_threads(thread_limit)
            except Exception:
                pass

        # Run math execution
        strategy = _InProcessRecursiveKMeansStrategy(
            stop_words=stop_words,
            max_folders=max_folders,
            max_depth=max_depth,
            max_features=max_features,
            pre_fetched_vectors=pre_fetched_vectors,
            filenames=filenames,
        )
        plan = strategy._cluster_recursive(filenames, documents, depth=1)
        output_queue.put({"status": "success", "plan": plan, "error": strategy._error})
    except Exception as e:
        import traceback
        try:
            output_queue.put({"status": "error", "error": str(e), "traceback": traceback.format_exc()})
        except Exception:
            pass


def is_gguf_model_dir(model_path: str) -> bool:
    """Check recursively if the given path contains any GGUF files."""
    if not model_path or not os.path.exists(model_path):
        return False
    for root, _, files in os.walk(model_path):
        for file in files:
            if file.lower().endswith(".gguf"):
                return True
    return False


def gguf_worker_main(model_path, input_queue, output_queue, n_threads=None):
    """Worker process main loop that handles local GGUF model generation."""
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

    def generate_plan(
        self,
        filenames: List[str],
        documents: List[str],
        max_folders: int,
        stop_words: set,
        max_depth: int = 5,
        max_features: int = 3,
        pre_fetched_vectors: List[list] | None = None,
        cancel_check: callable = None,
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
        )

        if not getattr(self, "_model_initialized", False):
            self._init_model()

        if self.generator is None and not (self._gguf_active and not self._gguf_failed):
            return plan, error

        doc_map = dict(zip(filenames, documents))

        def filter_plan(node, path_name=""):
            new_node = {}
            low_confidence_files = {}
            for k, v in node.items():
                if v is None:
                    doc_text = doc_map.get(k, "")[:1000]
                    prompt = f"Does this document about '{doc_text}' belong in a folder for '{path_name}'? Reply YES or NO."
                    validation_grammar = 'root ::= "YES" | "NO"'
                    try:
                        answer = (
                            self._run_prompt(prompt, 5, grammar=validation_grammar)
                            .strip()
                            .upper()
                        )

                        if "NO" in answer:
                            low_confidence_files[k] = None
                        else:
                            new_node[k] = None
                    except Exception as e:
                        logging.error(f"Coherence check failed: {e}")
                        new_node[k] = None
                elif isinstance(v, dict):
                    folder_name = k if not path_name else f"{path_name} {k}"
                    filtered_v, lc_v = filter_plan(v, path_name=folder_name)
                    if filtered_v:
                        new_node[k] = filtered_v
                    low_confidence_files.update(lc_v)
            return new_node, low_confidence_files

        with block_external_network():
            new_plan, lc_files = filter_plan(plan)

        if lc_files:
            if "Low Confidence" not in new_plan:
                new_plan["Low Confidence"] = {}
            new_plan["Low Confidence"].update(lc_files)

        return new_plan, error

    def __init__(self, model_path: str = None):
        self.generator = None
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
        if not documents:
            return "Miscellaneous"

        if not getattr(self, "_model_initialized", False):
            self._init_model()

        if self.generator is None and not (self._gguf_active and not self._gguf_failed):
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

        if db and base_dir:
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
                            import json

                            import numpy as np
                            from sklearn.metrics.pairwise import cosine_similarity

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
                            for filepath, user_verified_target, vector_str in rows:
                                dot_idx = filepath.rfind(".")
                                ext = (
                                    filepath[dot_idx:].lower() if dot_idx != -1 else ""
                                )
                                # Exclude non-textual attachments and image files from semantic similarity
                                if (
                                    ext in {".png", ".jpg", ".jpeg"}
                                    or ext not in supported_exts_set
                                ):
                                    continue

                                if vector_str:
                                    try:
                                        decrypted_vector_str = db.crypto.decrypt_vector(
                                            vector_str
                                        )
                                        v = json.loads(decrypted_vector_str)
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
                                    except Exception:
                                        db.track_corrupted_vector(base_dir, filepath)
                                        continue

                            if hist_vectors:
                                # 3. Cosine Similarity Calculation
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
                                        import os

                                        snippet = None
                                        if db and base_dir:
                                            try:
                                                ex_doc = db.get_document(
                                                    base_dir, ex["filepath"]
                                                )
                                                if ex_doc and ex_doc.get(
                                                    "extracted_text"
                                                ):
                                                    snippet = (
                                                        ex_doc["extracted_text"][:500]
                                                        .replace("\n", " ")
                                                        .strip()
                                                    )
                                            except Exception as e:
                                                logging.error(
                                                    f"Failed to fetch decrypted document snippet for semantic exemplar: {e}"
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

        if not use_semantic or not top_examples:
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
        return self._strategies.get(name)


clustering_registry = ClusteringRegistry()
clustering_registry.register("default", RecursiveKMeansStrategy())
clustering_registry.register("generative", GenerativeNamingStrategy())
