"""Core semantic analysis module.

This module provides topic modeling functionality.
"""

import contextlib
import hashlib
import logging
import os
import re
from collections import defaultdict
from typing import Any, cast

from pydantic import BaseModel

from app.core.analyzer_strategies import clustering_registry
from app.core.db_conn import DB_ERRORS
from app.core.domain_contracts import (
    SortingPlanNodeModel,
    validate_corpus_prefetch_batch,
    validate_jev_classification_result,
)
from app.core.path_utils import safe_relpath


class _Unspecified:
    """Sentinel type distinguishing an omitted parameter from explicit None."""

    __slots__ = ()


_UNSPECIFIED = _Unspecified()

_SortingPlanNodeSchema = SortingPlanNodeModel


def _extract_policy_priority(matched_pol: Any) -> int | None:
    """Extract an integer priority from a policy rule or matched_policies_map entry."""
    if matched_pol is None:
        return None
    raw_prio: Any
    if isinstance(matched_pol, dict):
        raw_prio = matched_pol.get("priority")
        if raw_prio is None and matched_pol.get("rule") is not None:
            rule_obj = matched_pol["rule"]
            raw_prio = (
                rule_obj.get("priority")
                if isinstance(rule_obj, dict)
                else getattr(rule_obj, "priority", None)
            )
    else:
        raw_prio = getattr(matched_pol, "priority", None)
    if isinstance(raw_prio, bool) or raw_prio is None:
        return None
    if isinstance(raw_prio, (int, float, str)):
        try:
            return int(raw_prio)
        except (ValueError, TypeError):
            return None
    return None


class SortingPlanNode(dict):
    """Pydantic-validated dict node representing a file or directory in a sorting plan."""

    @classmethod
    def model_validate(cls, obj: Any, *args, **kwargs) -> "SortingPlanNode":
        """Validate and construct a SortingPlanNode from a dictionary or instance."""
        if isinstance(obj, SortingPlanNode):
            return obj
        if isinstance(obj, BaseModel):
            obj = obj.model_dump(by_alias=True, exclude_none=True)
        if isinstance(obj, dict):
            validated = _SortingPlanNodeSchema.model_validate(obj).model_dump(
                by_alias=True, exclude_none=True
            )
            res = cls(obj)
            res.update(validated)
            return res
        raise ValueError(f"Cannot validate {type(obj)} as SortingPlanNode")

    def model_dump(self, *args, **kwargs) -> dict[str, Any]:
        """Dump the node data as a standard dictionary."""
        return dict(self)

    def dict(self, *args, **kwargs) -> dict[str, Any]:
        """Backward compatibility method for legacy callers."""
        return self.model_dump(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        """Provide dynamic attribute lookup for schema fields and dictionary keys."""
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(
                f"'{self.__class__.__name__}' object has no attribute '{name}'"
            )
        if name == "node_type":
            return self.get("node_type") or self.get("__type__", "file")
        if name in self:
            return self[name]
        if name in _SortingPlanNodeSchema.model_fields:
            return self.get(name)
        raise AttributeError(
            f"'{self.__class__.__name__}' object has no attribute '{name}'"
        )

    def __setattr__(self, name: str, value: Any) -> None:
        """Provide dynamic attribute assignment mapping to dictionary entries."""
        if name in ("node_type", "__type__"):
            self["__type__"] = value
            self["node_type"] = value
        else:
            self[name] = value


class SortingPlan(dict):
    """Pydantic-validated dict representing a complete hierarchical sorting plan."""

    def __init__(self, plan: dict[str, Any] | None = None, **kwargs):
        self.model_status = kwargs.pop(
            "model_status", getattr(plan, "model_status", "HEALTHY")
        )
        self.degradation_reason = kwargs.pop(
            "degradation_reason", getattr(plan, "degradation_reason", None)
        )
        self.recovery_action = kwargs.pop(
            "recovery_action", getattr(plan, "recovery_action", None)
        )
        self.suggested_recovery_action = kwargs.pop(
            "suggested_recovery_action",
            getattr(plan, "suggested_recovery_action", self.recovery_action),
        )
        self.failure_reason = kwargs.pop(
            "failure_reason", getattr(plan, "failure_reason", None)
        )
        self.invalid_locks = (
            kwargs.pop("invalid_locks", getattr(plan, "invalid_locks", {})) or {}
        )

        if plan is not None and isinstance(plan, dict):
            if "plan" in plan and isinstance(plan["plan"], dict) and len(plan) == 1:
                target = plan["plan"]
            else:
                target = plan
            validated = _validate_sorting_plan_nodes(target)
            super().__init__(validated)
        elif (
            kwargs
            and "plan" in kwargs
            and isinstance(kwargs["plan"], dict)
            and len(kwargs) == 1
        ):
            validated = _validate_sorting_plan_nodes(kwargs["plan"])
            super().__init__(validated)
        elif kwargs:
            validated = _validate_sorting_plan_nodes(kwargs)
            super().__init__(validated)
        else:
            super().__init__()

    def __getitem__(self, key: Any) -> Any:
        """Access dictionary items or fallback to diagnostic telemetry attributes."""
        if key in (
            "model_status",
            "degradation_reason",
            "recovery_action",
            "suggested_recovery_action",
            "failure_reason",
            "invalid_locks",
        ):
            if key in self:
                return super().__getitem__(key)
            return (
                getattr(self, key, {})
                if key == "invalid_locks"
                else getattr(self, key, None)
            )
        return super().__getitem__(key)

    def get(self, key: Any, default: Any = None) -> Any:
        """Get dictionary item or fallback to diagnostic telemetry attributes with default."""
        if key in (
            "model_status",
            "degradation_reason",
            "recovery_action",
            "suggested_recovery_action",
            "failure_reason",
            "invalid_locks",
        ):
            if key in self:
                return super().__getitem__(key)
            val = getattr(self, key, None)
            return val if val is not None else default
        return super().get(key, default)

    @classmethod
    def model_validate(cls, obj: Any, *args, **kwargs) -> "SortingPlan":
        """Validate and construct a SortingPlan from a dictionary or instance."""
        if isinstance(obj, SortingPlan):
            return obj
        invalid_locks = {}
        if isinstance(obj, BaseModel):
            invalid_locks = getattr(obj, "invalid_locks", {}) or {}
            obj = obj.model_dump(by_alias=True, exclude_none=True)
        if isinstance(obj, dict):
            if not invalid_locks and "invalid_locks" in obj:
                invalid_locks = obj.pop("invalid_locks", {})
            if "plan" in obj and isinstance(obj["plan"], dict) and len(obj) == 1:
                return cls(obj["plan"], invalid_locks=invalid_locks)
            return cls(obj, invalid_locks=invalid_locks)
        raise ValueError(f"Cannot validate {type(obj)} as SortingPlan")

    @property
    def plan(self) -> dict[str, Any]:
        """Backward compatibility property returning the plan dictionary."""
        return self

    @plan.setter
    def plan(self, value: dict[str, Any]) -> None:
        """Backward compatibility setter for setting plan dictionary content."""
        self.clear()
        if isinstance(value, dict):
            self.update(value)

    def model_dump(self, *args, **kwargs) -> dict[str, Any]:
        """Dump the complete plan as a standard dictionary."""
        res = {}
        for k, v in self.items():
            if hasattr(v, "model_dump"):
                res[k] = v.model_dump(*args, **kwargs)
            elif isinstance(v, dict):
                res[k] = _dump_node_recursive(v, *args, **kwargs)
            else:
                res[k] = v
        return res

    def dict(self, *args, **kwargs) -> dict[str, Any]:
        """Backward compatibility method for legacy callers."""
        return self.model_dump(*args, **kwargs)


def _dump_node_recursive(node: Any, *args, **kwargs) -> Any:
    if hasattr(node, "model_dump"):
        return node.model_dump(*args, **kwargs)
    if isinstance(node, dict):
        return {k: _dump_node_recursive(v, *args, **kwargs) for k, v in node.items()}
    return node


def _validate_sorting_plan_nodes(node: dict[str, Any]) -> dict[str, Any]:
    """Recursively validate all file dictionary nodes in the plan using SortingPlanNode."""
    validated = {}
    for k, v in node.items():
        if isinstance(v, (dict, SortingPlanNode, BaseModel)):
            v_dict = v.model_dump(by_alias=True) if hasattr(v, "model_dump") else v
            if (
                v_dict.get("__type__") == "file"
                or "relative_source" in v_dict
                or "routed_by" in v_dict
                or "status" in v_dict
            ):
                validated[k] = SortingPlanNode.model_validate(v_dict)
            else:
                validated[k] = _validate_sorting_plan_nodes(v_dict)
        else:
            validated[k] = v
    return validated


def pre_fetch_historical_corpus(
    db, base_dir, documents, pre_fetched_vectors=None, max_examples=50
):
    """Query, decrypt, and package up to 50 relevant historical folder examples from the database."""
    import logging

    import numpy as np

    # Retrieve model metadata from the database
    try:
        stored_signature = db.get_model_metadata("active_model_signature")
        stored_dimensions = db.get_model_metadata("active_model_dimensions")
        stored_version = db.get_model_metadata("active_model_version")
    except Exception as e:
        logging.error(
            f"Failed to fetch model metadata for pre-fetched corpus: {e}",
            exc_info=True,
        )
        stored_signature = None
        stored_dimensions = None
        stored_version = None

    model_metadata = {
        "active_model_signature": stored_signature,
        "active_model_dimensions": stored_dimensions,
        "active_model_version": stored_version,
    }

    if (
        not db
        or not base_dir
        or not callable(getattr(db, "get_verified_historical_vectors", None))
    ):
        return validate_corpus_prefetch_batch(
            {"model_metadata": model_metadata, "examples": []}
        )

    try:
        rows = list(
            db.get_verified_historical_vectors(
                base_dir,
                include_unvectorized=True,
                track_corrupted=True,
            )
        )
    except Exception as e:
        logging.error(
            f"Failed to query historical documents from DB: {e}", exc_info=True
        )
        return validate_corpus_prefetch_batch(
            {"model_metadata": model_metadata, "examples": []}
        )

    if not rows:
        return validate_corpus_prefetch_batch(
            {"model_metadata": model_metadata, "examples": []}
        )

    def _fetch_texts_map(filepaths: list[str]) -> dict[str, str]:
        if not filepaths or not callable(
            getattr(db, "get_documents_by_filepaths", None)
        ):
            return {}
        try:
            doc_rows = db.get_documents_by_filepaths(base_dir, filepaths)
        except Exception:
            logging.debug("Could not fetch historical example texts", exc_info=True)
            return {}
        texts: dict[str, str] = {}
        for fp, text in doc_rows:
            val = text if text is not None else ""
            texts[fp] = val
            norm_fp = fp.replace("\\", "/")
            if norm_fp not in texts:
                texts[norm_fp] = val
        return texts

    def _lookup_text(texts_map: dict[str, str], filepath: str) -> str:
        if filepath in texts_map:
            return texts_map[filepath]
        return texts_map.get(filepath.replace("\\", "/"), "")

    candidates: list[dict[str, Any]] = [
        {
            "filepath": filepath,
            "user_verified_target_path": user_verified_target_path,
            "vector": vector,
        }
        for filepath, user_verified_target_path, vector in rows
    ]

    # Restrict to maximum of 50 relevant historical examples
    selected_examples: list[dict[str, Any]] = []
    if len(candidates) <= max_examples:
        texts_map = _fetch_texts_map([c["filepath"] for c in candidates])
        selected_examples = [
            {
                "filepath": c["filepath"],
                "user_verified_target_path": c["user_verified_target_path"],
                "vector": c["vector"],
                "text": _lookup_text(texts_map, c["filepath"]),
            }
            for c in candidates
        ]
    else:
        # Rank candidates by relevance while deferring text decryption to selected examples
        ranked = False
        active_vectors = [v for v in (pre_fetched_vectors or []) if v is not None]

        # Attempt semantic ranking if active vectors and candidate vectors exist
        if active_vectors and any(c["vector"] is not None for c in candidates):
            try:
                centroid = np.mean(active_vectors, axis=0)
                from sklearn.metrics.pairwise import cosine_similarity

                cand_vectors: list[list[float]] = []
                cand_indices: list[int] = []

                for idx, c in enumerate(candidates):
                    parsed_v = c["vector"]
                    if parsed_v is not None and len(parsed_v) == len(centroid):
                        cand_vectors.append(parsed_v)
                        cand_indices.append(idx)

                if cand_vectors:
                    sims = cosine_similarity([centroid], cand_vectors).flatten()
                    sorted_cand_indices = [
                        cand_indices[i] for i in sims.argsort()[::-1]
                    ]
                    chosen_indices = list(sorted_cand_indices[:max_examples])
                    if len(chosen_indices) < max_examples:
                        selected_set = set(chosen_indices)
                        for idx in range(len(candidates)):
                            if idx not in selected_set:
                                chosen_indices.append(idx)
                                if len(chosen_indices) == max_examples:
                                    break

                    texts_map = _fetch_texts_map(
                        [candidates[idx]["filepath"] for idx in chosen_indices]
                    )
                    for idx in chosen_indices:
                        c = candidates[idx]
                        selected_examples.append(
                            {
                                "filepath": c["filepath"],
                                "user_verified_target_path": c[
                                    "user_verified_target_path"
                                ],
                                "vector": c["vector"],
                                "text": _lookup_text(texts_map, c["filepath"]),
                            }
                        )
                    ranked = True
            except Exception as e:
                logging.error(
                    f"Semantic ranking of historical examples failed: {e}",
                    exc_info=True,
                )

        if not ranked:
            # Fallback: TF-IDF ranking
            texts_map = {}
            try:
                from sklearn.feature_extraction.text import TfidfVectorizer
                from sklearn.metrics.pairwise import cosine_similarity

                from app.core.text_utils import sanitize_text

                texts_map = _fetch_texts_map([c["filepath"] for c in candidates])
                decrypted_texts = [
                    _lookup_text(texts_map, c["filepath"]) for c in candidates
                ]
                hist_texts = [sanitize_text(t) for t in decrypted_texts]
                active_text = sanitize_text(" ".join(documents or []))

                vectorizer = TfidfVectorizer(max_features=1000, stop_words="english")
                hist_vectors = vectorizer.fit_transform(hist_texts)
                active_vector = vectorizer.transform([active_text])

                sims = cosine_similarity(active_vector, hist_vectors).flatten()
                sorted_indices = sims.argsort()[::-1]

                selected_examples = []
                for idx in sorted_indices[:max_examples]:
                    c = candidates[idx]
                    selected_examples.append(
                        {
                            "filepath": c["filepath"],
                            "user_verified_target_path": c["user_verified_target_path"],
                            "vector": c["vector"],
                            "text": decrypted_texts[idx],
                        }
                    )
            except Exception as e:
                logging.error(
                    f"TF-IDF ranking of historical examples failed: {e}",
                    exc_info=True,
                )
                fallback_candidates = candidates[:max_examples]
                if not texts_map:
                    texts_map = _fetch_texts_map(
                        [c["filepath"] for c in fallback_candidates]
                    )
                selected_examples = [
                    {
                        "filepath": c["filepath"],
                        "user_verified_target_path": c["user_verified_target_path"],
                        "vector": c["vector"],
                        "text": _lookup_text(texts_map, c["filepath"]),
                    }
                    for c in fallback_candidates
                ]

    return validate_corpus_prefetch_batch(
        {"model_metadata": model_metadata, "examples": selected_examples}
    )


class IncrementalAnalyzer:
    """Stateful ML analyzer using incremental topic modeling."""

    def __init__(
        self,
        max_folders: int,
        stop_words: set,
        db,
        strategy_name: str = "generative",
        model_path: str | None | _Unspecified = _UNSPECIFIED,
    ) -> None:
        self.db = db
        self.max_folders = max_folders
        self.stop_words = stop_words
        self.strategy_name = strategy_name
        self.model_path: str | None = (
            None if isinstance(model_path, _Unspecified) else model_path
        )
        self.corpus = {}

        if isinstance(model_path, _Unspecified):
            self.model_path = None
            from app.core.path_utils import get_meipass_dir

            mei_dir = get_meipass_dir()
            if mei_dir:
                mei_bundle_path = os.path.join(mei_dir, "offline_bundle", "model")
                if os.path.exists(mei_bundle_path):
                    self.model_path = mei_bundle_path

            if not self.model_path:
                from app.config import get_app_dir
                from app.core.path_utils import get_base_path

                try:
                    base_path = get_base_path(__file__)
                except Exception:
                    logging.debug(
                        "Could not resolve base path; using package parent",
                        exc_info=True,
                    )
                    base_path = os.path.dirname(
                        os.path.dirname(os.path.abspath(__file__))
                    )
                local_bundle_path = os.path.join(base_path, "offline_bundle", "model")
                try:
                    user_bundle_path = str(get_app_dir() / "model")
                except Exception:
                    logging.debug(
                        "Could not resolve app dir; using home model path",
                        exc_info=True,
                    )
                    user_bundle_path = os.path.expanduser("~/.smart-autosorter/model")

                if os.path.exists(local_bundle_path):
                    self.model_path = local_bundle_path
                elif os.path.exists(user_bundle_path):
                    self.model_path = user_bundle_path

        from app.core.semantic_embeddings import (
            ModelValidationError,
            SemanticEmbeddingManager,
        )

        try:
            self.embedding_manager = SemanticEmbeddingManager(self.db, self.model_path)
            if getattr(self.embedding_manager, "is_degraded", False):
                self.model_path = self.embedding_manager.model_path
        except ModelValidationError as val_err:
            err_msg = str(val_err)
            recovery_action = (
                "INSTALL_ML_EXTRA"
                if "onnxruntime" in err_msg.lower()
                else "RE_DOWNLOAD_MODEL"
            )
            logging.warning(
                "Falling back to TF-IDF clustering after model validation failure: %s",
                err_msg,
            )
            from app.core.shared_registry import SharedModelRegistry

            SharedModelRegistry.get_instance().record_model_health(
                "onnx_embeddings",
                status="DEGRADED_FALLBACK",
                failure_reason=err_msg,
                suggested_recovery_action=recovery_action,
            )
            self.model_path = None
            self.embedding_manager = SemanticEmbeddingManager(self.db, None)
            self.embedding_manager.is_degraded = True
            self.embedding_manager.degradation_state = "DEGRADED_FALLBACK"
            self.embedding_manager.degradation_reason = err_msg
            self.embedding_manager.suggested_recovery_action = recovery_action

    def close(self):
        """Terminate processes."""
        self.terminate()

    def __del__(self):
        """Clean up."""
        self.terminate()

    def terminate(self):
        """Terminate processes."""
        if hasattr(self, "embedding_manager") and self.embedding_manager:
            try:
                self.embedding_manager.stop()
            except Exception:
                logging.debug("Embedding manager stop failed", exc_info=True)
        if getattr(self, "strategy_name", None):
            try:
                from app.core.analyzer_strategies import clustering_registry

                strategy = clustering_registry.get_strategy(self.strategy_name)
                if strategy and hasattr(strategy, "_fallback_to_pytorch"):
                    cast(Any, strategy)._fallback_to_pytorch()
            except Exception:
                logging.debug("Clustering strategy reset failed", exc_info=True)

    def partial_fit(self, base_dir: str, new_corpus: dict) -> None:
        """Update the ML model incrementally with new documents."""
        try:
            filepaths = []
            texts = []
            hashes = []
            for filepath, data in new_corpus.items():
                if isinstance(data, dict):
                    texts.append(data.get("text", ""))
                    hashes.append(data.get("hash", ""))
                else:
                    texts.append(data)
                    hashes.append("")
                filepaths.append(filepath)
                # Store key without retaining raw text string in memory post-extraction
                self.corpus[filepath] = None

            if not texts:
                return

            for i in range(len(filepaths)):
                if not hashes[i]:
                    hashes[i] = hashlib.md5(texts[i].encode("utf-8")).hexdigest()

            existing_hashes: dict[str, str] = {}
            if self.db:
                if hasattr(self.db, "get_document_hashes"):
                    existing_hashes = (
                        self.db.get_document_hashes(base_dir, filepaths) or {}
                    )
                else:
                    for fp in filepaths:
                        doc = self.db.get_document(base_dir, fp)
                        if doc and doc.get("file_hash"):
                            existing_hashes[fp] = doc.get("file_hash")

            documents_to_upsert = []
            for filepath, text, file_hash in zip(filepaths, texts, hashes):
                if (
                    self.db
                    and file_hash
                    and (
                        existing_hashes.get(filepath) == file_hash
                        or existing_hashes.get(filepath.replace("\\", "/")) == file_hash
                    )
                ):
                    continue

                documents_to_upsert.append(
                    (
                        base_dir,
                        filepath,
                        file_hash,
                        text,
                    )
                )

            if documents_to_upsert and self.db:
                self.db.upsert_documents(documents_to_upsert)

            # Immediately purge raw text string payloads post-extraction and DB persistence
            texts.clear()
            documents_to_upsert.clear()
            existing_hashes.clear()
            del texts, documents_to_upsert, existing_hashes

        except Exception as e:
            logging.error(f"Failed during partial_fit. Error: {str(e)}", exc_info=True)

    _FOLDER_SEMANTIC_EXPANSIONS: dict[str, str] = {
        "invoice": "invoice invoices billing bill bills statement payable charge payment remittance vendor receipt due",
        "bill": "bill bills billing invoice invoices statement utility payment due charge",
        "billing": "billing bill bills invoice invoices statement payment due charge",
        "receipt": "receipt receipts purchase order paid transaction expense reimbursement invoice",
        "tax": "tax taxes irs w2 1099 deduction return filing taxable revenue",
        "finance": "finance financial accounting ledger budget balance invoice billing tax bank statement",
        "financial": "financial finance accounting ledger budget balance invoice billing tax bank statement",
        "accounting": "accounting finance ledger invoice billing tax receipt",
        "contract": "contract contracts agreement nda legal clause terms signature addendum",
        "legal": "legal contract agreement nda clause compliance court",
        "resume": "resume cv curriculum vitae cover letter application employment experience",
        "medical": "medical health clinic prescription patient lab diagnosis insurance",
        "travel": "travel flight hotel itinerary booking boarding pass ticket reservation",
        "report": "report reports summary analysis quarterly annual metrics review",
    }

    @staticmethod
    def _stem_anchor_token(token: str) -> str:
        tok = token.lower().strip()
        if len(tok) <= 3:
            return tok
        if tok.endswith("ies") and len(tok) > 4:
            return tok[:-3] + "y"
        if tok.endswith(("ses", "xes", "zes", "ches", "shes")) and len(tok) > 4:
            return tok[:-2]
        if tok.endswith("s") and not tok.endswith(("ss", "us", "is")):
            tok = tok[:-1]
        if tok.endswith("ing") and len(tok) > 5:
            base = tok[:-3]
            if len(base) >= 4:
                return base
        return tok

    @classmethod
    def _augment_text_for_anchoring(cls, text: str) -> str:
        if not text:
            return ""
        split_camel = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
        raw_tokens = re.findall(r"[a-zA-Z0-9]{2,}", split_camel.lower())
        expanded: list[str] = []
        for tok in raw_tokens:
            expanded.append(tok)
            stem = cls._stem_anchor_token(tok)
            if stem and stem != tok:
                expanded.append(stem)
        return " ".join(expanded)

    @classmethod
    def _build_folder_anchor_profile(
        cls,
        folder_name: str,
        base_dir: str | None = None,
        dest_dir: str | None = None,
        exclude_rel_paths: set[str] | None = None,
    ) -> str:
        split_camel = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", folder_name)
        raw_tokens = re.findall(r"[a-zA-Z0-9]{2,}", split_camel.lower())
        parts: list[str] = []
        for tok in raw_tokens:
            parts.extend([tok, tok])
            stem = cls._stem_anchor_token(tok)
            if stem and stem != tok:
                parts.extend([stem, stem])
            for lookup in (tok, stem):
                if lookup in cls._FOLDER_SEMANTIC_EXPANSIONS:
                    parts.append(cls._FOLDER_SEMANTIC_EXPANSIONS[lookup])

        excl = exclude_rel_paths or set()
        for root in (base_dir, dest_dir):
            if not root:
                continue
            cand_dir = os.path.join(root, folder_name)
            if not os.path.isdir(cand_dir):
                continue
            try:
                sampled = 0
                with os.scandir(cand_dir) as it:
                    for entry in it:
                        if entry.name.startswith("."):
                            continue
                        rel_cand = f"{folder_name}/{entry.name}".replace("\\", "/")
                        if rel_cand in excl or rel_cand.lower() in excl:
                            continue
                        stem_name = os.path.splitext(entry.name)[0]
                        parts.append(cls._augment_text_for_anchoring(stem_name))
                        sampled += 1
                        if sampled >= 25:
                            break
            except OSError:
                pass

        return " ".join(p for p in parts if p)

    def _anchor_files_to_existing_folders(
        self,
        filenames: list[str],
        documents: list[str],
        existing_folders: list[str],
        base_dir: str | None,
        dest_dir: str | None,
        is_rejected_fn,
    ) -> tuple[list[tuple[str, str, str, str, str | None]], list[str], list[str]]:
        """Match candidate files against existing folders using ONNX and TF-IDF similarity."""
        if not filenames or not existing_folders:
            return [], filenames, documents

        import numpy as np
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity

        from app.core.scanner import parse_item_identifier

        excl_rels: set[str] = set()
        for fn in filenames:
            if fn:
                _, rel_fn = parse_item_identifier(fn, base_dir or "")
                excl_rels.add(rel_fn.replace("\\", "/").lower())
                excl_rels.add(fn.replace("\\", "/").lower())
        folder_profiles = [
            self._build_folder_anchor_profile(
                folder, base_dir, dest_dir, exclude_rel_paths=excl_rels
            )
            for folder in existing_folders
        ]
        candidate_profiles = [
            self._augment_text_for_anchoring(
                f"{os.path.splitext(os.path.basename(parse_item_identifier(fn, base_dir or '')[1]))[0]} {doc or ''}"
            )
            for fn, doc in zip(filenames, documents)
        ]

        n_files = len(filenames)
        n_folders = len(existing_folders)
        tfidf_sims = np.zeros((n_files, n_folders), dtype=np.float64)
        try:
            stop_list = list(self.stop_words) if self.stop_words else None
            vec = TfidfVectorizer(
                stop_words=stop_list,
                ngram_range=(1, 2),
                sublinear_tf=True,
                max_features=2000,
            )
            combined_matrix = vec.fit_transform(folder_profiles + candidate_profiles)
            folder_mat = combined_matrix[:n_folders]
            cand_mat = combined_matrix[n_folders:]
            tfidf_sims = np.nan_to_num(cosine_similarity(cand_mat, folder_mat), nan=0.0)
        except (ValueError, RuntimeError, TypeError):
            tfidf_sims = np.zeros((n_files, n_folders), dtype=np.float64)

        sims = tfidf_sims
        use_semantic = (
            getattr(self, "embedding_manager", None) is not None
            and not self.embedding_manager.is_mock
            and not self.embedding_manager.is_reconstruction_active()
        )
        if use_semantic and base_dir:
            try:
                file_vecs_dict = self.embedding_manager.get_vectors_batch(
                    base_dir, filenames, regenerate=True
                )
                anchor_vec_cache = getattr(self, "_folder_anchor_vec_cache", None)
                if anchor_vec_cache is None:
                    anchor_vec_cache = {}
                    self._folder_anchor_vec_cache = anchor_vec_cache
                em_id = id(self.embedding_manager)
                folder_vecs = []
                for fp in folder_profiles:
                    cache_key = (em_id, fp)
                    cached_vec = anchor_vec_cache.get(cache_key)
                    if cached_vec is None:
                        cached_vec = self.embedding_manager.generate_embedding(fp)
                        if cached_vec is not None:
                            anchor_vec_cache[cache_key] = cached_vec
                    folder_vecs.append(cached_vec)
                if folder_vecs and all(
                    self.embedding_manager.validate_vector_dimension(v)
                    for v in folder_vecs
                ):
                    D = self.embedding_manager.dimensions
                    file_vecs = []
                    valid_rows = []
                    for fn in filenames:
                        v = file_vecs_dict.get(fn.replace("\\", "/"))
                        if (
                            v is not None
                            and self.embedding_manager.validate_vector_dimension(v)
                        ):
                            file_vecs.append(v)
                            valid_rows.append(True)
                        else:
                            file_vecs.append([0.0] * D)
                            valid_rows.append(False)
                    sem_sims = np.nan_to_num(
                        cosine_similarity(np.array(file_vecs), np.array(folder_vecs)),
                        nan=0.0,
                    )
                    mask = np.array(valid_rows)[:, None]
                    sims = np.where(mask, np.maximum(sem_sims, tfidf_sims), tfidf_sims)
            except (ValueError, RuntimeError, TypeError, OSError):
                sims = tfidf_sims

        anchored: list[tuple[str, str, str, str, str | None]] = []
        rem_files: list[str] = []
        rem_docs: list[str] = []
        threshold = 0.18

        for idx, (fn, doc) in enumerate(zip(filenames, documents)):
            row = sims[idx]
            ranked = np.argsort(-row)
            chosen_folder = None
            chosen_score = 0.0
            for f_idx in ranked:
                score = float(row[f_idx])
                if score < threshold:
                    break
                cand_folder = existing_folders[int(f_idx)]
                if not is_rejected_fn(fn, cand_folder):
                    chosen_folder = cand_folder
                    chosen_score = score
                    break
            if chosen_folder is not None:
                anchored.append(
                    (
                        fn,
                        chosen_folder,
                        f"existing folder '{chosen_folder}' ({chosen_score:.2f})",
                        "existing_folder",
                        None,
                    )
                )
            else:
                rem_files.append(fn)
                rem_docs.append(doc)

        return anchored, rem_files, rem_docs

    def _snap_clusters_to_existing_folders(
        self,
        plan: dict,
        existing_folders: list[str],
        is_rejected_fn,
    ) -> dict:
        """Snap generated top-level cluster names to matching existing folders."""
        if not isinstance(plan, dict) or not existing_folders:
            return plan

        existing_by_lower = {ef.lower(): ef for ef in existing_folders if ef}
        existing_stems: dict[str, frozenset[str]] = {}
        for ef in existing_folders:
            tokens = re.findall(
                r"[a-zA-Z0-9]{2,}",
                re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", ef).lower(),
            )
            stems = frozenset(self._stem_anchor_token(t) for t in tokens if t)
            if stems:
                existing_stems[ef] = stems

        def _collect_leaf_keys(node: Any) -> list[str]:
            if not isinstance(node, dict):
                return []
            keys: list[str] = []
            for k, v in node.items():
                if v is None or (isinstance(v, dict) and v.get("__type__") == "file"):
                    keys.append(k)
                elif isinstance(v, dict):
                    keys.extend(_collect_leaf_keys(v))
            return keys

        def _deep_merge(dst: dict, src: dict) -> None:
            for k, v in src.items():
                if (
                    isinstance(v, dict)
                    and v.get("__type__") != "file"
                    and k in dst
                    and isinstance(dst[k], dict)
                    and dst[k].get("__type__") != "file"
                ):
                    _deep_merge(dst[k], v)
                else:
                    dst[k] = v

        snapped: dict = {}
        for cluster_key, cluster_val in plan.items():
            if (
                not isinstance(cluster_val, dict)
                or cluster_val.get("__type__") == "file"
                or cluster_key in ("Miscellaneous", "Review Required")
            ):
                snapped[cluster_key] = cluster_val
                continue

            matched_existing = existing_by_lower.get(cluster_key.lower())
            if matched_existing is None:
                c_tokens = re.findall(
                    r"[a-zA-Z0-9]{2,}",
                    re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", cluster_key).lower(),
                )
                c_stems = frozenset(self._stem_anchor_token(t) for t in c_tokens if t)
                if c_stems:
                    best_ef = None
                    best_overlap = 0.0
                    for ef, ef_stems in existing_stems.items():
                        inter = len(c_stems & ef_stems)
                        if inter > 0:
                            score = inter / max(1, min(len(c_stems), len(ef_stems)))
                            if score > best_overlap:
                                best_overlap = score
                                best_ef = ef
                    if best_ef is not None and best_overlap >= 0.5:
                        matched_existing = best_ef

            target_key = cluster_key
            if matched_existing is not None:
                leaf_keys = _collect_leaf_keys(cluster_val)
                if not any(is_rejected_fn(lk, matched_existing) for lk in leaf_keys):
                    target_key = matched_existing

            if target_key in snapped and isinstance(snapped[target_key], dict):
                _deep_merge(snapped[target_key], cluster_val)
            else:
                snapped[target_key] = cluster_val

        return snapped

    def _inject_hierarchy(self, node: dict) -> dict:
        """Transform a flat mapping of files into a nested folder structure based on relative paths."""
        if not isinstance(node, dict) or node.get("__type__") == "file":
            return node

        from app.core.scanner import parse_item_identifier

        default_base = getattr(self, "base_dir", "") or ""

        def _walk(curr_node: dict, parent_parts: tuple[str, ...] = ()) -> dict:
            if not isinstance(curr_node, dict) or curr_node.get("__type__") == "file":
                return curr_node

            new_node = {}
            for k, v in curr_node.items():
                if v is None or (isinstance(v, dict) and v.get("__type__") == "file"):
                    _, rel_k = parse_item_identifier(k, default_base)
                    norm_k = rel_k.replace("\\", "/")
                    dirname = os.path.dirname(norm_k).strip("/")
                    parts = [p for p in dirname.split("/") if p and p != "."]
                    if (
                        parent_parts
                        and len(parts) >= len(parent_parts)
                        and [p.lower() for p in parts[: len(parent_parts)]]
                        == [p.lower() for p in parent_parts]
                    ):
                        parts = parts[len(parent_parts) :]
                    if not parts:
                        new_node[k] = v
                    else:
                        current = new_node
                        for part in parts:
                            if (
                                part not in current
                                or current[part] is None
                                or (
                                    isinstance(current[part], dict)
                                    and current[part].get("__type__") == "file"
                                )
                            ):
                                current[part] = {}
                            current = current[part]
                        current[k] = v
                else:
                    child_prefix = (
                        () if os.path.isabs(k) or "::" in k else parent_parts + (k,)
                    )
                    new_node[k] = _walk(v, child_prefix)
            return new_node

        return _walk(node)

    def _build_sorting_plan(
        self,
        validated_plan: dict,
        invalid_locks: dict[str, str] | None = None,
    ) -> SortingPlan:
        """Construct a SortingPlan object and attach model health telemetry metadata."""
        from app.core.shared_registry import SharedModelRegistry

        registry = SharedModelRegistry.get_instance()
        health_records = registry.get_all_model_health()

        is_degraded = False
        degradation_reason = None
        recovery_action = None

        if getattr(self, "embedding_manager", None) and getattr(
            self.embedding_manager, "is_degraded", False
        ):
            is_degraded = True
            degradation_reason = getattr(
                self.embedding_manager,
                "degradation_reason",
                "Embedding model degraded",
            )
            recovery_action = getattr(
                self.embedding_manager,
                "suggested_recovery_action",
                "RE_DOWNLOAD_MODEL",
            )

        if not is_degraded and health_records:
            for model_id, record in health_records.items():
                if (
                    model_id == "generative_naming"
                    and getattr(self, "strategy_name", "default") != "generative"
                ):
                    continue
                status = record.get("status")
                if status in ("DEGRADED_FALLBACK", "FAILED"):
                    is_degraded = True
                    degradation_reason = (
                        record.get("failure_reason")
                        or f"Model '{model_id}' failed or degraded"
                    )
                    recovery_action = (
                        record.get("suggested_recovery_action") or "RE_DOWNLOAD_MODEL"
                    )
                    break

        plan_kwargs = {"plan": validated_plan}
        if invalid_locks:
            plan_kwargs["invalid_locks"] = invalid_locks
        if is_degraded:
            plan_kwargs["model_status"] = "DEGRADED_FALLBACK"
            plan_kwargs["degradation_reason"] = degradation_reason
            plan_kwargs["recovery_action"] = recovery_action
            plan_kwargs["suggested_recovery_action"] = recovery_action
            # Callers surface the degradation from these plan fields; core does
            # not reach into the UI to notify.

        return SortingPlan(**plan_kwargs)

    def generate_sorting_plan(
        self,
        base_dir: str,
        runtime_settings=None,
        locked_files: dict | None = None,
        cancel_check=None,
        fast_path_only: bool = False,
        target_paths: list[str] | None = None,
        jev_results: dict[str, Any] | None = None,
        source_dirs: list[str] | None = None,
        existing_folders: list[str] | None = None,
        dest_dir: str | None = None,
    ) -> SortingPlan:
        """Generate a sorting plan mapping file paths to destination paths based on current model state.

        Args:
            base_dir: Absolute path to the source root directory.
            runtime_settings: Optional application settings or keyword/learned rule overrides.
            locked_files: Dict mapping file paths to manually assigned target folder paths.
            cancel_check: Optional callable `Callable[[], bool]` invoked periodically during plan
                assembly loops. If `cancel_check()` returns `True`, execution aborts immediately
                and returns an empty SortingPlan object to honor token cancellation.
            fast_path_only: If `True`, skips hierarchical ML strategy execution and generates plan using
                rules and policies only.
            target_paths: Optional list of specific relative or absolute file paths to restrict plan generation to.
            jev_results: Optional dictionary mapping file paths to Jev classification results.

        Returns
        -------
            SortingPlan mapping relative file paths to target folder categories or absolute paths.
        """
        invalid_locks: dict[str, str] = {}
        valid_locked_files: dict[str, str] = {}
        if locked_files:
            from app.core.policy_engine import PolicyEngine

            for f, lock_path in locked_files.items():
                if lock_path:
                    is_valid, err_msg = PolicyEngine.validate_lock_path_safe(
                        lock_path, file_path=f
                    )
                    if is_valid:
                        valid_locked_files[f] = lock_path
                    else:
                        invalid_locks[f] = err_msg or "Invalid lock path"
            locked_files = valid_locked_files

        norm_target_paths = None
        if target_paths is not None:
            norm_target_paths = set()
            for p in target_paths:
                if not p:
                    continue
                if os.path.isabs(p) and base_dir and p.startswith(base_dir):
                    rel = safe_relpath(p, base_dir).replace("\\", "/")
                else:
                    rel = p.replace("\\", "/")
                norm_target_paths.add(rel)
                norm_target_paths.add(os.path.normpath(rel).replace("\\", "/"))

        try:
            if self.db and hasattr(self.db, "worker"):
                from app.core.db import DBWorker

                if isinstance(getattr(self.db, "worker", None), DBWorker):
                    with contextlib.suppress(RuntimeError, OSError):
                        self.db.worker.execute_write(lambda: None)

            docs = (
                self.db.get_all_documents(
                    base_dir, target_paths=norm_target_paths, source_dirs=source_dirs
                )
                if self.db
                else []
            )
            if norm_target_paths is not None and docs:
                docs = [
                    d
                    for d in docs
                    if d[0] in norm_target_paths
                    or os.path.normpath(d[0]).replace("\\", "/") in norm_target_paths
                ]
            if docs is None:
                docs = []

            from app.core.quarantine_interceptor import _is_content_approved_for_release
            from app.core.scanner import parse_item_identifier

            auto_quarantine_ratings = {
                str(r).upper()
                for r in (
                    getattr(runtime_settings, "AUTO_QUARANTINE_RATINGS", []) or []
                )
                if r
            }
            quarantine_dir_name = (
                getattr(
                    runtime_settings, "QUARANTINE_DIR_NAME", "_Compliance_Quarantine"
                )
                or "_Compliance_Quarantine"
            )
            qdir_top = quarantine_dir_name.replace("\\", "/").strip("/").split("/")[0]

            def _is_quarantine_rel_path(path_val: str) -> bool:
                if not path_val:
                    return False
                _, clean_rel = parse_item_identifier(str(path_val), base_dir)
                top_seg = clean_rel.replace("\\", "/").strip("/").split("/")[0]
                return (
                    top_seg.startswith(
                        ("_Quarantine_Staging", "_Compliance_Quarantine")
                    )
                    or bool(qdir_top)
                    and top_seg == qdir_top
                )

            if docs:
                docs = [d for d in docs if not _is_quarantine_rel_path(d[0])]
            if not docs and not jev_results:
                return self._build_sorting_plan({}, invalid_locks=invalid_locks)

            from app.core.extractor_strategies import registry
            from app.core.jev_classifier import JevClassifierEngine

            supported_exts = (
                set(registry._extractors.keys())
                | JevClassifierEngine.get_supported_extensions()
            )

            from app.core.metadata import (
                get_extension_category_target,
                matches_learned_rule_pattern,
            )
            from app.core.pattern_formatter import (
                PatternTokenFormatter,
                resolve_disk_file_date,
            )
            from app.core.policy_engine import PolicyEngine, PolicyEvaluationResult
            from app.core.rule_scopes import resolve_scoped_settings

            scope_rule_cache: dict[
                str, tuple[Any, list[Any], dict[Any, Any], dict[Any, Any]]
            ] = {}

            def _get_root_scope_rules(
                root_dir: str,
            ) -> tuple[Any, list[Any], dict[Any, Any], dict[Any, Any]]:
                cache_key = root_dir or ""
                cached_rules = scope_rule_cache.get(cache_key)
                if cached_rules is not None:
                    return cached_rules
                scoped_cfg = (
                    resolve_scoped_settings(
                        runtime_settings, cache_key, fallback_to_global=True
                    )
                    if (runtime_settings is not None and cache_key)
                    else runtime_settings
                )
                raw_kw = getattr(scoped_cfg, "KEYWORD_RULES", {}) if scoped_cfg else {}
                raw_learned = (
                    getattr(scoped_cfg, "LEARNED_RULES", {}) if scoped_cfg else {}
                )
                raw_pols = getattr(scoped_cfg, "POLICIES", []) if scoped_cfg else []
                bundle = (
                    scoped_cfg,
                    PolicyEngine.sort_policies(raw_pols),
                    raw_kw if isinstance(raw_kw, dict) else {},
                    raw_learned if isinstance(raw_learned, dict) else {},
                )
                scope_rule_cache[cache_key] = bundle
                return bundle

            _, sorted_policies, _, _ = _get_root_scope_rules(base_dir or "")

            from app.core.resilient_file_ops import (
                DETECT_ORIGINAL_BY_MODES,
                group_byte_identical_files,
            )

            detect_original_by = (
                getattr(runtime_settings, "DETECT_ORIGINAL_BY", "first_seen")
                if runtime_settings
                else "first_seen"
            )
            if detect_original_by == "first_seen":
                candidate_policy_lists: list[list[Any]] = [sorted_policies]
                seen_roots: set[str] = {base_dir or ""}
                for d_item in docs:
                    item_root, _ = parse_item_identifier(str(d_item[0]), base_dir)
                    eff_root = item_root or base_dir or ""
                    if eff_root not in seen_roots:
                        seen_roots.add(eff_root)
                        candidate_policy_lists.append(
                            _get_root_scope_rules(eff_root)[1]
                        )
                for pol_list in candidate_policy_lists:
                    found_mode = False
                    for pol in pol_list:
                        p_type = (
                            pol.type
                            if hasattr(pol, "type")
                            else pol.get("type")
                            if isinstance(pol, dict)
                            else None
                        )
                        p_sel = (
                            getattr(pol, "detect_original_by", None)
                            if hasattr(pol, "type")
                            else pol.get("detect_original_by")
                            if isinstance(pol, dict)
                            else None
                        )
                        if p_type == "duplicate" and p_sel in DETECT_ORIGINAL_BY_MODES:
                            detect_original_by = str(p_sel)
                            found_mode = True
                            break
                    if found_mode:
                        break

            doc_abs_map: dict[str, str] = {}
            abs_to_doc_key: dict[str, str] = {}
            ordered_abs_paths: list[str] = []
            for d in docs:
                f_key = str(d[0])
                s_root, s_rel = parse_item_identifier(f_key, base_dir)
                abs_f = os.path.normpath(os.path.abspath(os.path.join(s_root, s_rel)))
                doc_abs_map[f_key] = abs_f
                abs_to_doc_key.setdefault(abs_f, f_key)
                ordered_abs_paths.append(abs_f)

            byte_clusters = group_byte_identical_files(
                ordered_abs_paths, detect_original_by=detect_original_by
            )
            primary_by_abs: dict[str, tuple[str, str]] = {}
            primary_group_map: dict[str, str] = {}
            for cluster_idx, cluster in enumerate(byte_clusters, start=1):
                gid = f"dup_{cluster_idx}"
                orig_abs = os.path.normpath(os.path.abspath(cluster["original"]))
                primary_group_map[orig_abs] = gid
                for dup_abs in cluster["duplicates"]:
                    norm_dup = os.path.normpath(os.path.abspath(dup_abs))
                    primary_by_abs[norm_dup] = (orig_abs, gid)

            ai_filenames = []
            ai_documents = []
            policy_plan_files = []
            keyword_plan_files = []
            jev_plan_files = []
            unsupported_files = []
            historical_overrides = {}
            matched_policies_map = {}
            resolved_dates_map: dict[str, str] = {}
            abs_paths_map: dict[str, str] = {}
            folder_seq_counters: dict[str, int] = defaultdict(int)

            def _get_or_resolve_file_date(
                f_key: str,
                abs_path: str,
                matched_pol: Any | None = None,
            ) -> str:
                if f_key in resolved_dates_map:
                    return resolved_dates_map[f_key]
                pol_date = (
                    getattr(matched_pol, "resolved_date", None)
                    if matched_pol is not None
                    else None
                )
                if not pol_date and isinstance(matched_pol, dict):
                    pol_date = matched_pol.get("resolved_date")
                resolved = (
                    str(pol_date or "")
                    or resolve_disk_file_date(abs_path, base_dir=base_dir)
                    or ""
                )
                if resolved:
                    resolved_dates_map[f_key] = resolved
                return resolved

            def _expand_folder_template(
                raw_folder: str,
                f_key: str,
                abs_path: str,
                f_date: str,
            ) -> str:
                if not raw_folder or "{" not in raw_folder:
                    return raw_folder
                base_name = os.path.basename(f_key.split("::", 1)[-1])
                stem_val, ext_val = os.path.splitext(base_name)
                static_cat = next(
                    (
                        seg
                        for seg in raw_folder.replace("\\", "/").split("/")
                        if seg and "{" not in seg
                    ),
                    "uncategorized",
                )
                folder_seq_counters[raw_folder] += 1
                seq_num = folder_seq_counters[raw_folder]
                try:
                    return PatternTokenFormatter.format_target_path(
                        raw_folder,
                        metadata={
                            "date": f_date,
                            "category": static_cat,
                            "original": stem_val,
                            "stem": stem_val,
                            "extension": ext_val,
                            "base_dir": base_dir,
                        },
                        seq=seq_num,
                        file_path=abs_path,
                    )
                except ValueError as err:
                    logging.warning(
                        f"Failed to expand target folder template '{raw_folder}' for '{f_key}': {err}"
                    )
                    return "Miscellaneous"

            # Load document ratings and build per-file rejected target folder sets
            raw_ratings = (
                self.db.get_all_document_ratings(base_dir)
                if self.db and hasattr(self.db, "get_all_document_ratings")
                else {}
            )
            doc_ratings: dict[str, str] = (
                {
                    str(k).replace("\\", "/"): str(v).lower()
                    for k, v in raw_ratings.items()
                    if v
                }
                if isinstance(raw_ratings, dict)
                else {}
            )
            rejected_targets: dict[str, set[str]] = defaultdict(set)
            for d in docs:
                norm_df = str(d[0]).replace("\\", "/")
                raw_assigned = d[3] if len(d) > 3 else None
                rating_val = doc_ratings.get(norm_df, "")
                if isinstance(raw_assigned, str) and raw_assigned.startswith("!"):
                    rej = raw_assigned[1:].strip().replace("\\", "/").strip("/")
                    if rej:
                        rejected_targets[norm_df].add(rej.lower())
                        rejected_targets[norm_df].add(rej.split("/")[0].lower())
                elif rating_val == "negative" and raw_assigned:
                    rej = str(raw_assigned).strip().replace("\\", "/").strip("/")
                    if rej:
                        rejected_targets[norm_df].add(rej.lower())
                        rejected_targets[norm_df].add(rej.split("/")[0].lower())

            def _is_target_rejected(f_path: str, candidate: str | None) -> bool:
                if not candidate:
                    return False
                rej_set = rejected_targets.get(str(f_path).replace("\\", "/"))
                if not rej_set:
                    return False
                cand_norm = str(candidate).strip().replace("\\", "/").strip("/").lower()
                if not cand_norm:
                    return False
                return cand_norm in rej_set or cand_norm.split("/")[0] in rej_set

            def _clean_verified_folder(val: str | None) -> str | None:
                if val is None:
                    return None
                s = str(val).strip().replace("\\", "/")
                if not s or s in (".", "./"):
                    return None
                stripped = s.strip("/")
                if not stripped or stripped == ".":
                    return None
                return str(val).strip()

            if locked_files:
                locked_files = {
                    k: v
                    for k, v in locked_files.items()
                    if _clean_verified_folder(v) is not None
                    and not _is_target_rejected(k, v)
                }

            # Map file hashes to their historical targets (excluding quarantine and rejected targets)
            hash_to_target = {}
            for d in docs:
                if cancel_check and cancel_check():
                    return SortingPlan()
                norm_df = str(d[0]).replace("\\", "/")
                clean_d3 = _clean_verified_folder(d[3] if len(d) > 3 else None)
                if (
                    clean_d3 is not None
                    and not clean_d3.startswith("!")
                    and doc_ratings.get(norm_df) != "negative"
                    and not _is_quarantine_rel_path(clean_d3)
                ):
                    hash_to_target[d[2]] = clean_d3

            verified_target_names: dict[str, str] = {}
            if self.db and hasattr(self.db, "get_user_verified_target_names"):
                try:
                    raw_vt_names = self.db.get_user_verified_target_names(
                        base_dir, source_dirs=source_dirs
                    )
                    if isinstance(raw_vt_names, dict):
                        for k_vt, v_vt in raw_vt_names.items():
                            if isinstance(v_vt, str) and v_vt.strip():
                                clean_vt = v_vt.strip()
                                verified_target_names[str(k_vt)] = clean_vt
                                verified_target_names[str(k_vt).replace("\\", "/")] = (
                                    clean_vt
                                )
                except (*DB_ERRORS, OSError, RuntimeError, ValueError, TypeError):
                    verified_target_names = {}

            for d in docs:
                if cancel_check and cancel_check():
                    return SortingPlan()
                f, doc = d[0], d[1]
                norm_f_key = str(f).replace("\\", "/")
                file_hash = d[2] if len(d) > 2 else None
                assigned_folder = _clean_verified_folder(d[3] if len(d) > 3 else None)
                if assigned_folder is not None and (
                    str(assigned_folder).startswith("!")
                    or doc_ratings.get(norm_f_key) == "negative"
                    or _is_quarantine_rel_path(assigned_folder)
                    or _is_target_rejected(f, assigned_folder)
                ):
                    assigned_folder = None

                cand_hash_target = hash_to_target.get(file_hash)
                if _is_target_rejected(f, cand_hash_target):
                    cand_hash_target = None

                target = (
                    assigned_folder if assigned_folder is not None else cand_hash_target
                )

                filename_only = os.path.basename(f).lower()
                doc_lower = doc.lower() if doc else ""

                status_match = None
                if doc and doc.startswith("[STATUS:"):
                    status_match = doc[8:-1]

                ext = os.path.splitext(f)[1].lower()
                if ext not in supported_exts and not status_match:
                    status_match = "UNSUPPORTED"

                # Check if this file has a path lock / manual override first!
                locked_target = None
                if locked_files:
                    locked_target = _clean_verified_folder(
                        locked_files.get(f)
                        if f in locked_files
                        else locked_files.get(norm_f_key)
                    )
                if locked_target is not None:
                    target = locked_target

                src_root, rel_f = parse_item_identifier(f, base_dir)
                owning_root = src_root or base_dir or ""
                (
                    scoped_file_settings,
                    file_sorted_policies,
                    keyword_rules,
                    learned_rules,
                ) = _get_root_scope_rules(owning_root)
                abs_f = (
                    os.path.join(src_root, rel_f)
                    if src_root and not os.path.isabs(rel_f)
                    else rel_f
                )
                abs_src_root = os.path.abspath(src_root) if src_root else ""
                abs_paths_map[f] = abs_f

                # Check against unified policies first!
                # return_halting=True always yields a (result, halt) tuple.
                matched_policy, halt_evaluation = cast(
                    "tuple[PolicyEvaluationResult | None, bool]",
                    PolicyEngine.evaluate_policies(
                        f,
                        doc,
                        status_match,
                        file_sorted_policies,
                        return_halting=True,
                        is_sorted=True,
                        abs_file_path=abs_f,
                        is_duplicate=(doc_abs_map.get(str(f), "") in primary_by_abs),
                    ),
                )

                if matched_policy:
                    from app.core.quarantine_interceptor import (
                        _is_redactable_extension,
                    )

                    raw_pol_target = (
                        matched_policy.target_path
                        if hasattr(matched_policy, "target_path")
                        else matched_policy.get("target_path")
                    )
                    pol_expr = (
                        matched_policy.expression
                        if hasattr(matched_policy, "expression")
                        else matched_policy.get("expression", "")
                    )
                    pol_type = (
                        matched_policy.type
                        if hasattr(matched_policy, "type")
                        else matched_policy.get("type", "policy")
                    )
                    pol_rename_pat = getattr(
                        matched_policy, "rename_pattern", None
                    ) or (
                        matched_policy.get("rename_pattern")
                        if isinstance(matched_policy, dict)
                        else None
                    )
                    pol_action_raw = getattr(matched_policy, "action", None) or (
                        matched_policy.get("action")
                        if isinstance(matched_policy, dict)
                        else None
                    )
                    pol_action = (
                        str(pol_action_raw).strip().lower() if pol_action_raw else None
                    )
                    pol_priority = _extract_policy_priority(matched_policy)
                    post_unarchive_act = (
                        getattr(matched_policy, "post_unarchive_action", None)
                        or (
                            matched_policy.get("post_unarchive_action")
                            if isinstance(matched_policy, dict)
                            else None
                        )
                        or getattr(matched_policy, "post_action", None)
                        or (
                            matched_policy.get("post_action")
                            if isinstance(matched_policy, dict)
                            else None
                        )
                        or getattr(matched_policy, "archive_action", None)
                        or (
                            matched_policy.get("archive_action")
                            if isinstance(matched_policy, dict)
                            else None
                        )
                    )
                    post_unarchive_tgt = (
                        getattr(matched_policy, "post_unarchive_target", None)
                        or (
                            matched_policy.get("post_unarchive_target")
                            if isinstance(matched_policy, dict)
                            else None
                        )
                        or getattr(matched_policy, "archive_target", None)
                        or (
                            matched_policy.get("archive_target")
                            if isinstance(matched_policy, dict)
                            else None
                        )
                    )

                    quarantine_dir_cfg = (
                        getattr(
                            runtime_settings,
                            "QUARANTINE_DIR_NAME",
                            "_Compliance_Quarantine",
                        )
                        if runtime_settings
                        else "_Compliance_Quarantine"
                    ) or "_Compliance_Quarantine"

                    cleaned_pol_target = str(raw_pol_target or "").strip()
                    if pol_action == "quarantine":
                        resolved_pol_target = cleaned_pol_target or quarantine_dir_cfg
                    elif pol_action == "redact":
                        if not _is_redactable_extension(f):
                            resolved_pol_target = quarantine_dir_cfg
                        else:
                            resolved_pol_target = (
                                cleaned_pol_target or "Redacted_Documents"
                            )
                    elif pol_action == "archive":
                        resolved_pol_target = cleaned_pol_target or "Archive"
                    elif pol_action == "retain":
                        resolved_pol_target = cleaned_pol_target or "Retained_Documents"
                    elif pol_action == "unarchive":
                        resolved_pol_target = cleaned_pol_target
                    else:
                        resolved_pol_target = cleaned_pol_target

                    f_date = _get_or_resolve_file_date(f, abs_f, matched_policy)
                    if resolved_pol_target:
                        resolved_pol_target = _expand_folder_template(
                            resolved_pol_target, f, abs_f, f_date
                        )
                    resolved_post_unarchive_tgt = (
                        _expand_folder_template(
                            str(post_unarchive_tgt).strip(), f, abs_f, f_date
                        )
                        if post_unarchive_tgt is not None
                        else None
                    )

                    policy_plan_files.append(
                        (
                            f,
                            resolved_pol_target,
                            pol_expr,
                            pol_type,
                            status_match,
                            pol_action,
                            str(post_unarchive_act).strip().lower()
                            if post_unarchive_act
                            else None,
                            resolved_post_unarchive_tgt,
                        )
                    )
                    matched_policies_map[f] = {
                        "rule": matched_policy,
                        "type": pol_type,
                        "expression": pol_expr,
                        "target_path": resolved_pol_target,
                        "rename_pattern": pol_rename_pat,
                        "resolved_date": f_date,
                        "priority": pol_priority,
                        "action": pol_action,
                        "policy_action": pol_action,
                        "post_unarchive_action": (
                            str(post_unarchive_act).strip().lower()
                            if post_unarchive_act
                            else None
                        ),
                        "post_unarchive_target": resolved_post_unarchive_tgt,
                    }
                    if target is not None:
                        historical_overrides[f] = (target, status_match)
                    continue

                if halt_evaluation:
                    if status_match:
                        unsupported_files.append((f, status_match))
                    else:
                        ai_filenames.append(f)
                        ai_documents.append(doc)
                    continue

                if locked_target is not None and target is not None:
                    historical_overrides[f] = (target, status_match)
                    continue

                matched = False
                jev_res = None
                if jev_results:
                    norm_f = f.replace("\\", "/")
                    base_fn = os.path.basename(f)
                    for k_jev, v_jev in jev_results.items():
                        norm_k = str(k_jev).replace("\\", "/")
                        if (
                            norm_k == norm_f
                            or norm_k == base_fn
                            or norm_f.endswith(norm_k)
                            or norm_k.endswith(norm_f)
                        ):
                            jev_res = v_jev
                            break

                if jev_res:
                    jev_model = validate_jev_classification_result(jev_res)
                    sens_upper = str(jev_model.sensitivity_rating or "LOW").upper()
                    if (
                        sens_upper in auto_quarantine_ratings
                        and not _is_content_approved_for_release(
                            self.db,
                            base_dir=abs_src_root,
                            file_path=abs_f,
                        )
                    ):
                        jev_plan_files.append(
                            (f, quarantine_dir_name, jev_model, status_match)
                        )
                        continue
                    if target is not None:
                        historical_overrides[f] = (target, status_match)
                    if (
                        jev_model.is_classified
                        and jev_model.confidence > 0.0
                        and jev_model.category != "Unclassified"
                    ):
                        jev_plan_files.append(
                            (f, jev_model.category, jev_model, status_match)
                        )
                        matched = True
                elif auto_quarantine_ratings:
                    try:
                        from app.core.shared_registry import SharedModelRegistry

                        jev_engine = (
                            SharedModelRegistry.get_instance().get_jev_classifier()
                        )
                        if jev_engine:
                            auto_jev_res = jev_engine.classify(
                                abs_f,
                                text_content=None if status_match else doc,
                            )
                            if auto_jev_res:
                                auto_jev_model = validate_jev_classification_result(
                                    auto_jev_res
                                )
                                sens_upper = str(
                                    auto_jev_model.sensitivity_rating or "LOW"
                                ).upper()
                                if (
                                    sens_upper in auto_quarantine_ratings
                                    and not _is_content_approved_for_release(
                                        self.db,
                                        base_dir=abs_src_root,
                                        file_path=abs_f,
                                    )
                                ):
                                    jev_plan_files.append(
                                        (
                                            f,
                                            quarantine_dir_name,
                                            auto_jev_model,
                                            status_match,
                                        )
                                    )
                                    continue
                    except Exception as e:
                        logging.warning(
                            f"Auto-quarantine Jev sensitivity evaluation failed for '{f}': {e}",
                            exc_info=True,
                        )
                    if target is not None:
                        historical_overrides[f] = (target, status_match)
                else:
                    if target is not None:
                        historical_overrides[f] = (target, status_match)

                if not matched and keyword_rules:
                    for keyword, target_folder in keyword_rules.items():
                        if not keyword.strip() or _is_target_rejected(f, target_folder):
                            continue
                        text_to_search = (
                            filename_only
                            if status_match
                            else (filename_only + " " + doc_lower)
                        )
                        if keyword.lower() in text_to_search:
                            f_date = _get_or_resolve_file_date(f, abs_f)
                            exp_folder = _expand_folder_template(
                                target_folder, f, abs_f, f_date
                            )
                            keyword_plan_files.append(
                                (f, exp_folder, keyword, "keyword", status_match)
                            )
                            matched = True
                            break

                if not matched and learned_rules:
                    text_to_search = (
                        filename_only
                        if status_match
                        else (filename_only + " " + doc_lower)
                    )
                    norm_search = re.sub(r"[_\-\s]+", " ", text_to_search)
                    for keyword, target_folder in learned_rules.items():
                        if not str(keyword).strip() or _is_target_rejected(
                            f, target_folder
                        ):
                            continue
                        if matches_learned_rule_pattern(
                            str(keyword), text_to_search, norm_search=norm_search
                        ):
                            f_date = _get_or_resolve_file_date(f, abs_f)
                            exp_folder = _expand_folder_template(
                                target_folder, f, abs_f, f_date
                            )
                            keyword_plan_files.append(
                                (f, exp_folder, keyword, "pattern", status_match)
                            )
                            matched = True
                            break

                if not matched and (status_match or ext not in supported_exts):
                    cat_target = get_extension_category_target(f, scoped_file_settings)
                    if cat_target:
                        keyword_plan_files.append(
                            (f, cat_target, ext, "extension_category", status_match)
                        )
                        matched = True

                if not matched:
                    if status_match:
                        unsupported_files.append((f, status_match))
                    else:
                        ai_filenames.append(f)
                        ai_documents.append(doc)

            if jev_results:
                processed_files = {
                    str(f).replace("\\", "/") for f, _, _, _ in jev_plan_files
                }
                for d_row in docs:
                    raw_df = str(d_row[0]).replace("\\", "/")
                    processed_files.add(raw_df)
                    processed_files.add(
                        parse_item_identifier(raw_df, base_dir)[1].replace("\\", "/")
                    )
                for k_jev, v_jev in jev_results.items():
                    f_path = str(k_jev)
                    src_r, rel_r = parse_item_identifier(f_path, base_dir)
                    norm_rel_r = rel_r.replace("\\", "/")
                    if (
                        not _is_quarantine_rel_path(f_path)
                        and f_path.replace("\\", "/") not in processed_files
                        and norm_rel_r not in processed_files
                        and os.path.basename(f_path) not in processed_files
                    ):
                        v_jev_model = validate_jev_classification_result(v_jev)
                        sens_upper = str(
                            v_jev_model.sensitivity_rating or "LOW"
                        ).upper()
                        abs_r = (
                            os.path.join(src_r, rel_r)
                            if src_r and not os.path.isabs(rel_r)
                            else rel_r
                        )
                        if (
                            sens_upper in auto_quarantine_ratings
                            and not _is_content_approved_for_release(
                                self.db,
                                base_dir=os.path.abspath(src_r) if src_r else "",
                                file_path=abs_r,
                            )
                        ):
                            jev_plan_files.append(
                                (f_path, quarantine_dir_name, v_jev_model, None)
                            )
                            processed_files.add(f_path.replace("\\", "/"))
                            processed_files.add(norm_rel_r)
                        elif (
                            v_jev_model.is_classified
                            and v_jev_model.confidence > 0.0
                            and v_jev_model.category != "Unclassified"
                        ):
                            jev_plan_files.append(
                                (f_path, v_jev_model.category, v_jev_model, None)
                            )
                            processed_files.add(f_path.replace("\\", "/"))
                            processed_files.add(norm_rel_r)

            # Internal Jev Classifier Fallback execution for unclassified candidate documents in fast-path or for non-extractor files
            if ai_filenames and (
                fast_path_only
                or any(
                    not registry.is_supported(os.path.splitext(fn)[1].lower())
                    for fn in ai_filenames
                )
            ):
                unclassified_docs_map = (
                    {d[0]: d[1] for d in docs if len(d) > 1} if docs else {}
                )
                processed_jev_files = {
                    str(f_item).replace("\\", "/") for f_item, _, _, _ in jev_plan_files
                }
                remaining_ai_files = []
                remaining_ai_docs = []

                for idx, f in enumerate(ai_filenames):
                    norm_f = f.replace("\\", "/")
                    base_fn = os.path.basename(norm_f)

                    # Historical user assignments take precedence over internal Jev fallback
                    if f in historical_overrides or norm_f in historical_overrides:
                        remaining_ai_files.append(f)
                        remaining_ai_docs.append(ai_documents[idx])
                        continue

                    is_in_caller_jev = False
                    if jev_results:
                        for k_jev in jev_results.keys():
                            norm_k = str(k_jev).replace("\\", "/")
                            if (
                                norm_k == norm_f
                                or norm_k == base_fn
                                or norm_f.endswith(norm_k)
                                or norm_k.endswith(norm_f)
                            ):
                                is_in_caller_jev = True
                                break

                    if not is_in_caller_jev and norm_f not in processed_jev_files:
                        try:
                            from app.core.shared_registry import SharedModelRegistry

                            jev_engine = (
                                SharedModelRegistry.get_instance().get_jev_classifier()
                            )
                            if jev_engine:
                                abs_f = (
                                    os.path.join(base_dir, f)
                                    if base_dir and not os.path.isabs(f)
                                    else f
                                )
                                doc_text = (
                                    ai_documents[idx]
                                    if idx < len(ai_documents)
                                    else unclassified_docs_map.get(f)
                                )
                                fallback_res = jev_engine.classify(
                                    abs_f, text_content=doc_text
                                )
                                if fallback_res:
                                    is_classified = (
                                        getattr(fallback_res, "is_classified", False)
                                        if not isinstance(fallback_res, dict)
                                        else fallback_res.get("is_classified", False)
                                    )
                                    confidence = (
                                        getattr(fallback_res, "confidence", 0.0)
                                        if not isinstance(fallback_res, dict)
                                        else fallback_res.get("confidence", 0.0)
                                    )
                                    cat = (
                                        getattr(
                                            fallback_res, "category", "Unclassified"
                                        )
                                        if not isinstance(fallback_res, dict)
                                        else fallback_res.get(
                                            "category", "Unclassified"
                                        )
                                    )

                                    if (
                                        is_classified
                                        and confidence > 0.0
                                        and cat != "Unclassified"
                                    ):
                                        jev_plan_files.append(
                                            (f, cat, fallback_res, None)
                                        )
                                        processed_jev_files.add(norm_f)
                                        continue
                        except Exception as e:
                            logging.warning(
                                f"Internal Jev classification fallback failed for '{f}': {e}",
                                exc_info=True,
                            )

                    remaining_ai_files.append(f)
                    remaining_ai_docs.append(ai_documents[idx])

                ai_filenames = remaining_ai_files
                ai_documents = remaining_ai_docs

            if fast_path_only:
                ai_filenames = []
                ai_documents = []
                unsupported_files = []

            # Route unclassified files without text extractor strategies to unsupported_files before generative AI / similarity fallback
            if ai_filenames:
                from app.core.extractor_strategies import registry

                filtered_ai_filenames = []
                filtered_ai_documents = []
                for f, doc in zip(ai_filenames, ai_documents):
                    ext = os.path.splitext(f)[1].lower()
                    if not registry.is_supported(ext):
                        unsupported_files.append((f, "UNSUPPORTED"))
                    else:
                        filtered_ai_filenames.append(f)
                        filtered_ai_documents.append(doc)
                ai_filenames = filtered_ai_filenames
                ai_documents = filtered_ai_documents

            # Document-to-Document Content Similarity Matching Phase
            historical_docs = []
            for d in docs:
                norm_df = str(d[0]).replace("\\", "/")
                file_hash = d[2] if len(d) > 2 else None
                assigned_folder = _clean_verified_folder(d[3] if len(d) > 3 else None)
                if assigned_folder is not None and (
                    str(assigned_folder).startswith("!")
                    or doc_ratings.get(norm_df) == "negative"
                    or _is_quarantine_rel_path(assigned_folder)
                ):
                    assigned_folder = None
                target = (
                    assigned_folder
                    if assigned_folder is not None
                    else hash_to_target.get(file_hash)
                )
                if _is_target_rejected(d[0], target):
                    target = None

                if target is not None and d[1]:
                    from app.core.extractor_strategies import registry

                    ext = os.path.splitext(d[0])[1].lower()
                    if (
                        ext not in {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".webp"}
                        and registry.is_supported(ext)
                        and not d[1].startswith("[STATUS:")
                        and d[1].strip()
                    ):
                        historical_docs.append(
                            {"text": d[1], "target_folder": target, "filepath": d[0]}
                        )

            if historical_docs and ai_filenames:
                try:
                    import numpy as np
                    from sklearn.metrics.pairwise import cosine_similarity

                    from app.core.db_conn import get_db_connection

                    # Check if embedding reconstruction is active or mock state is detected
                    is_mock = self.embedding_manager.is_mock
                    is_reconstruction_active = (
                        self.embedding_manager.is_reconstruction_active()
                    )
                    use_semantic = not is_mock and not is_reconstruction_active

                    # 1. Fetch existing vectors from DB batch-wise (regenerating active on-the-fly if semantic is enabled)
                    hist_filepaths = [doc["filepath"] for doc in historical_docs]

                    if use_semantic:
                        ai_vectors_dict = self.embedding_manager.get_vectors_batch(
                            base_dir, ai_filenames, regenerate=True
                        )
                        hist_vectors_dict = self.embedding_manager.get_vectors_batch(
                            base_dir, hist_filepaths, regenerate=False
                        )
                    else:
                        ai_vectors_dict = {}
                        hist_vectors_dict = {}

                    # 2. Check each vector's validity and trigger background reconstruction if any are invalid/missing
                    D = self.embedding_manager.dimensions
                    ai_vectors_list = []
                    is_ai_vector_valid = []
                    any_invalid_or_missing = False
                    corrupted_vector_paths = []

                    for f_name in ai_filenames:
                        v = ai_vectors_dict.get(f_name.replace("\\", "/"))
                        if (
                            v is not None
                            and self.embedding_manager.validate_vector_dimension(v)
                        ):
                            ai_vectors_list.append(v)
                            is_ai_vector_valid.append(True)
                        else:
                            ai_vectors_list.append([0.0] * D)
                            is_ai_vector_valid.append(False)
                            if use_semantic:
                                any_invalid_or_missing = True
                                self.db.track_corrupted_vector(base_dir, f_name)
                                corrupted_vector_paths.append(f_name.replace("\\", "/"))

                    hist_vectors_list = []
                    is_hist_vector_valid = []
                    for doc in historical_docs:
                        fp = doc["filepath"]
                        v = hist_vectors_dict.get(fp.replace("\\", "/"))
                        if (
                            v is not None
                            and self.embedding_manager.validate_vector_dimension(v)
                        ):
                            hist_vectors_list.append(v)
                            is_hist_vector_valid.append(True)
                        else:
                            hist_vectors_list.append([0.0] * D)
                            is_hist_vector_valid.append(False)
                            if use_semantic:
                                any_invalid_or_missing = True
                                self.db.track_corrupted_vector(base_dir, fp)
                                corrupted_vector_paths.append(fp.replace("\\", "/"))

                    if use_semantic and corrupted_vector_paths:

                        def _delete_corrupted(paths=list(corrupted_vector_paths)):
                            conn = get_db_connection(self.db.db_path)
                            with conn:
                                conn.executemany(
                                    "DELETE FROM document_vectors WHERE base_dir = ? AND filepath = ?",
                                    [(base_dir, p) for p in paths],
                                )

                        self.db.worker.execute_write(_delete_corrupted)

                    # Queue missing/invalid vectors for background reconstruction asynchronously without blocking
                    if use_semantic and any_invalid_or_missing:
                        self.embedding_manager.trigger_reconstruction(base_dir)

                    # 3. Compute TF-IDF keyword-frequency similarity matrix
                    import math

                    from scipy.sparse import csr_matrix
                    from sklearn.feature_extraction.text import (
                        CountVectorizer,
                        TfidfTransformer,
                    )
                    from sklearn.preprocessing import normalize

                    # Database TF-IDF Statistics Retrieval
                    N, top_terms, doc_terms, _doc_metadata = self.db.get_tfidf_stats(
                        base_dir
                    )

                    # Restrict vocabulary to the top 1,000 terms matching the database query constraints
                    top_terms = top_terms[:1000]

                    # Construct the vocabulary dictionary mapping
                    vocab = {term: idx for idx, (term, df) in enumerate(top_terms)}

                    if len(vocab) == 0:
                        keyword_similarities = np.zeros(
                            (len(ai_documents), len(historical_docs))
                        )
                    else:
                        # Calculate smoothed IDF values using the system's custom formula: idf_j = ln((1 + N) / (1 + df_j)) + 1
                        idf_weights = {
                            term: math.log((1 + N) / (1 + df)) + 1
                            for term, df in top_terms
                        }
                        idf_values = np.array(
                            [idf_weights[term] for term, df in top_terms]
                        )

                        # Configure CountVectorizer and TfidfTransformer
                        count_vectorizer = CountVectorizer(
                            stop_words=list(self.stop_words),
                            vocabulary=vocab,
                        )
                        transformer = TfidfTransformer(sublinear_tf=True)
                        transformer.idf_ = idf_values

                        # Historical Sparse Matrix Reconstruction
                        # Represent historical document vectors by constructing a standard scipy.sparse.csr_matrix directly from database term statistics.
                        hist_filepaths_norm = [
                            doc["filepath"].replace("\\", "/")
                            for doc in historical_docs
                        ]
                        filepath_to_row_idx = {
                            fp: idx for idx, fp in enumerate(hist_filepaths_norm)
                        }

                        rows = []
                        cols = []
                        data = []

                        cached_matrix_rows = self.db.get_tfidf_matrix_cache(base_dir)
                        if cached_matrix_rows:
                            for filepath, term, weight in cached_matrix_rows:
                                norm_fp = filepath.replace("\\", "/")
                                if norm_fp in filepath_to_row_idx and term in vocab:
                                    rows.append(filepath_to_row_idx[norm_fp])
                                    cols.append(vocab[term])
                                    data.append(weight)
                        else:
                            for filepath, term, tf in doc_terms:
                                norm_fp = filepath.replace("\\", "/")
                                if norm_fp in filepath_to_row_idx:
                                    row_idx = filepath_to_row_idx[norm_fp]
                                    if term in vocab:
                                        col_idx = vocab[term]
                                        tf_weight = 1.0 + math.log(max(1, tf))
                                        weight = tf_weight * idf_weights[term]
                                        rows.append(row_idx)
                                        cols.append(col_idx)
                                        data.append(weight)
                            self.db.update_tfidf_matrix_cache(base_dir)

                        num_rows = len(historical_docs)
                        num_cols = len(vocab)
                        historical_vectors = csr_matrix(
                            (data, (rows, cols)), shape=(num_rows, num_cols)
                        )

                        # Apply row-wise L2 normalization to ensure correct cosine similarity calculations
                        historical_vectors = normalize(
                            historical_vectors, norm="l2", axis=1
                        )

                        # Vectorizing Active Candidate Documents
                        from app.core.text_utils import sanitize_text

                        safe_ai_documents = [
                            sanitize_text(d or "") for d in ai_documents
                        ]
                        counts = count_vectorizer.transform(safe_ai_documents)
                        new_docs_vectors = transformer.transform(counts)

                        # Dot Product Similarity Calculation
                        keyword_similarities = new_docs_vectors.dot(
                            historical_vectors.T
                        ).toarray()

                    # 4. Compute semantic (cosine) similarity matrix & merge scores
                    if (
                        use_semantic
                        and len(ai_vectors_list) > 0
                        and len(hist_vectors_list) > 0
                    ):
                        semantic_similarities = cosine_similarity(
                            np.array(ai_vectors_list), np.array(hist_vectors_list)
                        )
                        semantic_similarities = np.nan_to_num(
                            semantic_similarities, nan=0.0
                        )

                        ai_mask = np.array(is_ai_vector_valid)[:, None]
                        hist_mask = np.array(is_hist_vector_valid)[None, :]
                        valid_mask = ai_mask & hist_mask
                        similarities = np.where(
                            valid_mask, semantic_similarities, keyword_similarities
                        )
                    else:
                        similarities = keyword_similarities.copy()

                    # Ensure merged scores stay within 0.0 - 1.0 range
                    similarities = np.clip(similarities, 0.0, 1.0)

                    historical_targets = [
                        doc["target_folder"] for doc in historical_docs
                    ]

                    remaining_ai_filenames = []
                    remaining_ai_documents = []

                    for i, f in enumerate(ai_filenames):
                        if len(historical_docs) > 0:
                            ranked_hist = np.argsort(-similarities[i])
                            matched_hist_folder = None
                            matched_hist_sim = 0.0
                            for doc_idx in ranked_hist:
                                cand_sim = float(similarities[i][doc_idx])
                                if cand_sim < 0.8:
                                    break
                                cand_target = historical_targets[int(doc_idx)]
                                if not _is_target_rejected(f, cand_target):
                                    matched_hist_folder = cand_target
                                    matched_hist_sim = cand_sim
                                    break
                            if matched_hist_folder is not None:
                                keyword_plan_files.append(
                                    (
                                        f,
                                        matched_hist_folder,
                                        f"similarity >= 0.8 ({matched_hist_sim:.2f})",
                                        "similarity",
                                        None,
                                    )
                                )
                            else:
                                remaining_ai_filenames.append(f)
                                remaining_ai_documents.append(ai_documents[i])
                        else:
                            remaining_ai_filenames.append(f)
                            remaining_ai_documents.append(ai_documents[i])

                    ai_filenames = remaining_ai_filenames
                    ai_documents = remaining_ai_documents
                except Exception as e:
                    logging.error(
                        f"Failed during document-to-document similarity matching. Error: {str(e)}",
                        exc_info=True,
                    )

            # Discover and anchor candidate files to existing folders on disk
            from app.core.scanner import discover_existing_folders

            if existing_folders is not None:
                active_existing_folders: list[str] = []
                seen_ef: set[str] = set()
                for ef in existing_folders:
                    if ef and ef.lower() not in seen_ef:
                        seen_ef.add(ef.lower())
                        active_existing_folders.append(ef)
            else:
                disk_base = base_dir if base_dir and os.path.isdir(base_dir) else None
                disk_dest = dest_dir if dest_dir and os.path.isdir(dest_dir) else None
                discovered_ef = discover_existing_folders(
                    disk_base, dest_dir=disk_dest, settings=runtime_settings
                )
                model_abs = (
                    os.path.abspath(str(self.model_path))
                    if getattr(self, "model_path", None)
                    else None
                )
                if model_abs:
                    active_existing_folders = []
                    for ef in discovered_ef:
                        is_model_dir = False
                        for root_cand in (disk_base, disk_dest):
                            if not root_cand:
                                continue
                            ef_abs = os.path.abspath(os.path.join(root_cand, ef))
                            if model_abs == ef_abs or model_abs.startswith(
                                ef_abs + os.sep
                            ):
                                is_model_dir = True
                                break
                        if not is_model_dir:
                            active_existing_folders.append(ef)
                else:
                    active_existing_folders = discovered_ef

            if active_existing_folders and ai_filenames and not fast_path_only:
                anchored_items, ai_filenames, ai_documents = (
                    self._anchor_files_to_existing_folders(
                        ai_filenames,
                        ai_documents,
                        active_existing_folders,
                        base_dir if base_dir and os.path.isdir(base_dir) else None,
                        dest_dir if dest_dir and os.path.isdir(dest_dir) else None,
                        _is_target_rejected,
                    )
                )
                if anchored_items:
                    keyword_plan_files.extend(anchored_items)

            if active_existing_folders and unsupported_files and not fast_path_only:
                unsup_fns = [uf for uf, _ in unsupported_files]
                unsup_docs = ["" for _ in unsupported_files]
                unsup_status_map = dict(unsupported_files)
                anchored_unsup, rem_unsup_fns, _ = (
                    self._anchor_files_to_existing_folders(
                        unsup_fns,
                        unsup_docs,
                        active_existing_folders,
                        base_dir if base_dir and os.path.isdir(base_dir) else None,
                        dest_dir if dest_dir and os.path.isdir(dest_dir) else None,
                        _is_target_rejected,
                    )
                )
                if anchored_unsup:
                    for fn, tgt, match_desc, r_by, _ in anchored_unsup:
                        keyword_plan_files.append(
                            (fn, tgt, match_desc, r_by, unsup_status_map.get(fn))
                        )
                    rem_set = set(rem_unsup_fns)
                    unsupported_files = [
                        (uf, st) for uf, st in unsupported_files if uf in rem_set
                    ]

            active_strategy_name = self.strategy_name if not fast_path_only else None
            if (
                active_strategy_name
                and active_strategy_name != "generative"
                and self.embedding_manager.is_mock
            ):
                active_strategy_name = "default"

            if active_strategy_name:
                max_depth = (
                    getattr(runtime_settings, "MAX_DEPTH", 5) if runtime_settings else 5
                )
                max_features = (
                    getattr(runtime_settings, "MAX_FEATURES", 3)
                    if runtime_settings
                    else 3
                )

                strategy = clustering_registry.get_strategy(active_strategy_name)
                if strategy:
                    if hasattr(strategy, "set_db_context"):
                        cast(Any, strategy).set_db_context(self.db, base_dir)
                    else:
                        cast(Any, strategy).db = self.db
                        cast(Any, strategy).base_dir = base_dir

                    use_semantic = not self.embedding_manager.is_mock
                    pre_fetched_vectors = None

                    if use_semantic and ai_filenames:
                        try:
                            # Batch retrieve and/or generate active file vectors
                            ai_vectors_dict = self.embedding_manager.get_vectors_batch(
                                base_dir, ai_filenames, regenerate=True
                            )
                            vectors = []
                            for f_name in ai_filenames:
                                v = ai_vectors_dict.get(f_name.replace("\\", "/"))
                                if (
                                    v is not None
                                    and self.embedding_manager.validate_vector_dimension(
                                        v
                                    )
                                ):
                                    vectors.append(v)
                                else:
                                    vectors.append(None)
                            pre_fetched_vectors = vectors
                        except Exception as e:
                            logging.error(
                                f"Lazy semantic vector generation/caching failed: {e}. Falling back to standard TF-IDF.",
                                exc_info=True,
                            )
                            pre_fetched_vectors = None

                    # Pre-fetch and serialize historical folder examples to prevent database lockups in child process
                    pre_fetched_corpus = None
                    if self.db:
                        try:
                            raw_corpus = pre_fetch_historical_corpus(
                                db=self.db,
                                base_dir=base_dir,
                                documents=ai_documents,
                                pre_fetched_vectors=pre_fetched_vectors,
                                max_examples=50,
                            )
                            if raw_corpus:
                                pre_fetched_corpus = validate_corpus_prefetch_batch(
                                    raw_corpus
                                )
                        except Exception as e:
                            logging.error(
                                f"Failed to pre-fetch historical corpus: {e}",
                                exc_info=True,
                            )

                    if ai_filenames:
                        plan, _ = strategy.generate_plan(
                            ai_filenames,
                            ai_documents,
                            self.max_folders,
                            self.stop_words,
                            max_depth,
                            max_features,
                            pre_fetched_vectors=pre_fetched_vectors,
                            cancel_check=cancel_check,
                            pre_fetched_corpus=pre_fetched_corpus,
                        )
                        if hasattr(plan, "plan") and isinstance(plan.plan, dict):
                            plan = plan.plan
                    else:
                        plan = {}
                        if active_strategy_name == "generative" and hasattr(
                            strategy, "_record_degraded_fallback"
                        ):
                            from app.core.analyzer_strategies import (
                                is_prompt_dump_enabled,
                            )

                            if (
                                not getattr(strategy, "_model_initialized", False)
                                and getattr(strategy, "_generator", None) is None
                                and not getattr(strategy, "_gguf_active", False)
                                and not is_prompt_dump_enabled()
                            ):
                                m_path = getattr(strategy, "model_path", None)
                                if not m_path or not os.path.exists(m_path):
                                    cast(Any, strategy)._record_degraded_fallback(
                                        f"Model path does not exist: '{m_path}'"
                                    )
                                else:
                                    try:
                                        from app.core.exceptions import (
                                            HashVerificationError,
                                        )
                                        from app.core.shared_registry import (
                                            SharedModelRegistry,
                                        )

                                        SharedModelRegistry.get_instance().verify_integrity(
                                            "generative_naming", m_path
                                        )
                                    except (
                                        OSError,
                                        ValueError,
                                        RuntimeError,
                                        HashVerificationError,
                                    ) as e:
                                        cast(Any, strategy)._record_degraded_fallback(
                                            str(e)
                                        )

                    # Data Boundary Safeguard: Clear/Garbage collect pre_fetched_corpus decrypted data immediately
                    if pre_fetched_corpus:
                        pre_fetched_corpus.clear()
                        del pre_fetched_corpus
                        pre_fetched_corpus = None

                    if active_existing_folders and plan:
                        plan = self._snap_clusters_to_existing_folders(
                            plan, active_existing_folders, _is_target_rejected
                        )

                    if rejected_targets and isinstance(plan, dict):
                        relocated_rejected: list[tuple[str, Any]] = []
                        for top_folder, sub_dict in list(plan.items()):
                            if not isinstance(sub_dict, dict):
                                continue
                            for fk, fv in list(sub_dict.items()):
                                is_leaf = fv is None or (
                                    isinstance(fv, dict)
                                    and fv.get("__type__") == "file"
                                )
                                if is_leaf and _is_target_rejected(fk, top_folder):
                                    relocated_rejected.append((fk, sub_dict.pop(fk)))
                            if not sub_dict:
                                plan.pop(top_folder, None)
                        for fk, fv in relocated_rejected:
                            alt_folder = (
                                "Review Required"
                                if not _is_target_rejected(fk, "Review Required")
                                else "Miscellaneous"
                            )
                            plan.setdefault(alt_folder, {})[fk] = fv
                else:
                    plan = {}
            else:
                plan = {}

            rename_seq_counters: dict[str, int] = defaultdict(int)
            policy_target_filenames: dict[str, str] = {}

            def _compute_policy_rename(f_key: str, target_fld: str) -> str | None:
                if f_key in policy_target_filenames:
                    return policy_target_filenames[f_key]
                matched_pol = matched_policies_map.get(f_key)
                if not matched_pol:
                    return None
                ren_pat = getattr(matched_pol, "rename_pattern", None) or (
                    matched_pol.get("rename_pattern")
                    if isinstance(matched_pol, dict)
                    else None
                )
                if not ren_pat or not str(ren_pat).strip():
                    return None
                abs_path = abs_paths_map.get(f_key, "")
                f_date = _get_or_resolve_file_date(f_key, abs_path, matched_pol)
                base_name = os.path.basename(f_key.split("::", 1)[-1])
                stem_val, ext_val = os.path.splitext(base_name)
                cat_leaf = (
                    target_fld.replace("\\", "/").strip("/").split("/")[-1]
                    if target_fld
                    else "uncategorized"
                )
                rename_seq_counters[target_fld] += 1
                seq_num = rename_seq_counters[target_fld]
                formatted_name = PatternTokenFormatter.format_pattern(
                    str(ren_pat).strip(),
                    metadata={
                        "date": f_date,
                        "category": cat_leaf or "uncategorized",
                        "original": stem_val,
                        "stem": stem_val,
                        "extension": ext_val,
                        "base_dir": base_dir,
                    },
                    seq=seq_num,
                    fallback_original=base_name,
                    file_path=abs_path,
                )
                if formatted_name:
                    policy_target_filenames[f_key] = formatted_name
                return formatted_name or None

            # Inject policy routed files back into the plan
            for (
                f,
                target_folder,
                expression,
                rule_type,
                ext_status,
                pol_action,
                post_unarchive_act,
                post_unarchive_tgt,
            ) in policy_plan_files:
                if cancel_check and cancel_check():
                    return SortingPlan()
                pol_priority = _extract_policy_priority(matched_policies_map.get(f))
                pol_node_info: dict[str, Any] = {
                    "__type__": "file",
                    "routed_by": rule_type,
                    "keyword": expression,
                    "extraction_status": ext_status,
                }
                f_date = resolved_dates_map.get(f)
                if f_date:
                    pol_node_info["file_date"] = f_date
                if pol_priority is not None:
                    pol_node_info["policy_priority"] = pol_priority
                if pol_action:
                    pol_node_info["policy_action"] = pol_action
                    if pol_action == "trash":
                        pol_node_info["duplicate_action"] = "trash"
                if post_unarchive_act:
                    pol_node_info["post_unarchive_action"] = post_unarchive_act
                if post_unarchive_tgt is not None:
                    pol_node_info["post_unarchive_target"] = post_unarchive_tgt
                if pol_action not in ("trash", "quarantine", "unarchive"):
                    pol_renamed = _compute_policy_rename(f, target_folder)
                    if pol_renamed:
                        pol_node_info["target_filename"] = pol_renamed
                        pol_node_info["is_confirmed"] = True
                        pol_node_info["user_confirmed"] = True

                norm_tf = target_folder.replace("\\", "/").strip("/")
                if not norm_tf or norm_tf == ".":
                    plan.setdefault("", {})[f] = pol_node_info
                    continue
                parts = [p for p in norm_tf.split("/") if p]
                current = plan
                for i, part in enumerate(parts):
                    if part not in current:
                        current[part] = {}
                    if not isinstance(current[part], dict):
                        current[part] = {"_original": current[part]}
                    if i == len(parts) - 1:
                        current[part][f] = dict(pol_node_info)
                    else:
                        current = current[part]

            # Inject keyword routed files back into the plan
            for f, target_folder, keyword, routed_by, ext_status in keyword_plan_files:
                if cancel_check and cancel_check():
                    return SortingPlan()
                parts = target_folder.replace("\\", "/").split("/")
                current = plan
                for i, part in enumerate(parts):
                    if part not in current:
                        current[part] = {}
                    if not isinstance(current[part], dict):
                        current[part] = {"_original": current[part]}
                    if i == len(parts) - 1:
                        current[part][f] = {
                            "__type__": "file",
                            "routed_by": routed_by,
                            "keyword": keyword,
                            "extraction_status": ext_status,
                        }
                    else:
                        current = current[part]

            # Inject Jev routed files back into the plan
            for f, target_folder, jev_res, ext_status in jev_plan_files:
                if cancel_check and cancel_check():
                    return SortingPlan()
                parts = target_folder.replace("\\", "/").split("/")
                current = plan
                for i, part in enumerate(parts):
                    if part not in current:
                        current[part] = {}
                    if not isinstance(current[part], dict):
                        current[part] = {"_original": current[part]}
                    if i == len(parts) - 1:
                        cat_val = jev_res.category
                        sens_val = jev_res.sensitivity_rating
                        sens_score = jev_res.sensitivity_score
                        arch_prio = jev_res.archival_priority
                        arch_score = jev_res.archival_priority_score
                        conf_val = jev_res.confidence

                        node_data = {
                            "__type__": "file",
                            "routed_by": "jev_classifier",
                            "match": str(cat_val) if cat_val else None,
                            "extraction_status": ext_status,
                            "category": cat_val,
                            "sensitivity_rating": sens_val,
                            "sensitivity_score": sens_score,
                            "archival_priority": arch_prio,
                            "archival_priority_score": arch_score,
                            "confidence": conf_val,
                        }
                        node_model = SortingPlanNodeModel.model_validate(
                            {k: v for k, v in node_data.items() if v is not None}
                        )
                        current[part][f] = SortingPlanNode.model_validate(node_model)
                    else:
                        current = current[part]

            if unsupported_files:
                if "Miscellaneous" not in plan:
                    plan["Miscellaneous"] = {}
                elif not isinstance(plan["Miscellaneous"], dict):
                    plan["Miscellaneous"] = {"_original": plan["Miscellaneous"]}
                for f, ext_status in unsupported_files:
                    plan["Miscellaneous"][f] = {
                        "__type__": "file",
                        "routed_by": "fallback",
                        "match": "none",
                        "extraction_status": ext_status,
                    }

            def _is_file_leaf(val: Any) -> bool:
                if val is None or isinstance(val, SortingPlanNode):
                    return True
                if hasattr(val, "node_type") and getattr(val, "node_type") == "file":
                    return True
                if isinstance(val, dict):
                    return bool(
                        val.get("__type__") == "file"
                        or val.get("routed_by")
                        or val.get("extraction_status")
                    )
                return False

            def remove_from_plan(node, target_f):
                removed = None
                norm_target_f = str(target_f).replace("\\", "/")
                for k, v in list(node.items()):
                    norm_k = str(k).replace("\\", "/")
                    if k == target_f or norm_k == norm_target_f:
                        if _is_file_leaf(v):
                            removed = node.pop(k)
                            continue
                        if isinstance(v, dict) and "_original" in v:
                            removed = v.pop("_original")
                            if not v:
                                node.pop(k)
                            continue
                    if isinstance(v, dict) and not _is_file_leaf(v):
                        res = remove_from_plan(v, target_f)
                        if res is not None:
                            removed = res
                        if not v:
                            node.pop(k)
                return removed

            if locked_files is None:
                locked_files = {}
            norm_locked_files = {
                str(k).replace("\\", "/"): v for k, v in locked_files.items()
            }

            # Phase 1: Policy, Keyword, and Learned Rule sorting
            compliance_targets = {}
            rule_info_map = {}
            for (
                f,
                target_folder,
                expression,
                rule_type,
                status,
                _pol_action,
                _post_act,
                _post_tgt,
            ) in policy_plan_files:
                compliance_targets[f] = target_folder
                compliance_targets[f.replace("\\", "/")] = target_folder
                rule_info_map[f] = (rule_type, expression)
                rule_info_map[f.replace("\\", "/")] = (rule_type, expression)
            for f, target_folder, keyword, routed_by, ext_status in keyword_plan_files:
                compliance_targets[f] = target_folder
                compliance_targets[f.replace("\\", "/")] = target_folder
                rule_info_map[f] = (routed_by, keyword)
                rule_info_map[f.replace("\\", "/")] = (routed_by, keyword)
            for f, target_folder, jev_res, ext_status in jev_plan_files:
                compliance_targets[f] = target_folder
                compliance_targets[f.replace("\\", "/")] = target_folder
                rule_info_map[f] = (
                    "jev_classifier",
                    getattr(jev_res, "category", None),
                )
                rule_info_map[f.replace("\\", "/")] = (
                    "jev_classifier",
                    getattr(jev_res, "category", None),
                )

            for (
                f,
                target_folder,
                expression,
                rule_type,
                status,
                pol_action,
                post_unarchive_act,
                post_unarchive_tgt,
            ) in policy_plan_files:
                if cancel_check and cancel_check():
                    return SortingPlan()
                if target_folder not in plan:
                    plan[target_folder] = {}
                pol_priority = _extract_policy_priority(matched_policies_map.get(f))
                pol_flat_info: dict[str, Any] = {
                    "__type__": "file",
                    "routed_by": rule_type,
                    "match": expression,
                    "status": status,
                }
                f_date = resolved_dates_map.get(f)
                if f_date:
                    pol_flat_info["file_date"] = f_date
                if pol_priority is not None:
                    pol_flat_info["policy_priority"] = pol_priority
                if pol_action:
                    pol_flat_info["policy_action"] = pol_action
                    if pol_action == "trash":
                        pol_flat_info["duplicate_action"] = "trash"
                if post_unarchive_act:
                    pol_flat_info["post_unarchive_action"] = post_unarchive_act
                if post_unarchive_tgt is not None:
                    pol_flat_info["post_unarchive_target"] = post_unarchive_tgt
                if pol_action not in ("trash", "quarantine", "unarchive"):
                    pol_renamed = _compute_policy_rename(f, target_folder)
                    if pol_renamed:
                        pol_flat_info["target_filename"] = pol_renamed
                        pol_flat_info["is_confirmed"] = True
                        pol_flat_info["user_confirmed"] = True
                plan[target_folder][f] = pol_flat_info

            for f, target_folder, keyword, rule_type, status in keyword_plan_files:
                if cancel_check and cancel_check():
                    return SortingPlan()
                if target_folder not in plan:
                    plan[target_folder] = {}
                plan[target_folder][f] = {
                    "__type__": "file",
                    "routed_by": rule_type,
                    "match": keyword,
                    "status": status,
                }

            # Inject Historical Assignments and handle conflicts
            for f, (target_folder, status) in historical_overrides.items():
                if cancel_check and cancel_check():
                    return SortingPlan()
                target_folder = _clean_verified_folder(target_folder)
                if target_folder is None:
                    continue

                norm_f = str(f).replace("\\", "/")
                has_verified_name = bool(
                    verified_target_names.get(f) or verified_target_names.get(norm_f)
                )
                is_explicit_lock = (
                    f in locked_files
                    or norm_f in norm_locked_files
                    or has_verified_name
                )
                explicit_lock_target = (
                    locked_files.get(f)
                    if f in locked_files
                    else (
                        norm_locked_files.get(norm_f)
                        or (target_folder if has_verified_name else None)
                    )
                )

                is_conflicted = False
                is_overridden_lock = False
                compliance_path = compliance_targets.get(
                    f, compliance_targets.get(norm_f)
                )
                original_hist_path = target_folder
                original_lock_path = target_folder
                used_rule_routing = False

                if compliance_path is not None and compliance_path != target_folder:
                    if f in matched_policies_map:
                        # Compliance policy always takes absolute precedence!
                        is_conflicted = True
                        is_overridden_lock = True
                        original_lock_path = target_folder
                        target_folder = compliance_path
                    elif is_explicit_lock and explicit_lock_target:
                        # Explicit user lock stays ahead of non-policy rules (CONTEXT.md:L43-L45)
                        target_folder = explicit_lock_target
                        if target_folder != compliance_path:
                            is_conflicted = True
                    else:
                        # Fixed rules (keyword, learned, extension category, sensitivity) stay ahead of unlocked history
                        is_conflicted = True
                        original_hist_path = target_folder
                        target_folder = compliance_path
                        used_rule_routing = True
                elif (
                    compliance_path is not None
                    and compliance_path == target_folder
                    and not is_explicit_lock
                ):
                    used_rule_routing = True

                # Remove from all prior locations (including nested folders like Finance/2025)
                remove_from_plan(plan, f)

                if target_folder not in plan:
                    plan[target_folder] = {}

                info = {
                    "__type__": "file",
                    "status": status,
                }

                matched_pol = matched_policies_map.get(f)
                matched_rule = rule_info_map.get(f, rule_info_map.get(norm_f))
                if matched_pol:
                    info["routed_by"] = getattr(matched_pol, "type", None) or (
                        matched_pol.get("type", "policy")
                        if isinstance(matched_pol, dict)
                        else "policy"
                    )
                    info["match"] = getattr(matched_pol, "expression", None) or (
                        matched_pol.get("expression")
                        if isinstance(matched_pol, dict)
                        else None
                    )
                    pol_priority = _extract_policy_priority(matched_pol)
                    if pol_priority is not None:
                        info["policy_priority"] = pol_priority
                    pol_act_override = (
                        matched_pol.get("policy_action") or matched_pol.get("action")
                        if isinstance(matched_pol, dict)
                        else getattr(matched_pol, "action", None)
                    )
                    if pol_act_override:
                        info["policy_action"] = pol_act_override
                        if pol_act_override == "trash":
                            info["duplicate_action"] = "trash"
                    post_act_override = (
                        matched_pol.get("post_unarchive_action")
                        if isinstance(matched_pol, dict)
                        else getattr(matched_pol, "post_unarchive_action", None)
                    )
                    if post_act_override:
                        info["post_unarchive_action"] = post_act_override
                    post_tgt_override = (
                        matched_pol.get("post_unarchive_target")
                        if isinstance(matched_pol, dict)
                        else getattr(matched_pol, "post_unarchive_target", None)
                    )
                    if post_tgt_override is not None:
                        info["post_unarchive_target"] = post_tgt_override
                    f_date = resolved_dates_map.get(f)
                    if f_date:
                        info["file_date"] = f_date
                    if pol_act_override not in ("trash", "quarantine", "unarchive"):
                        pol_renamed = _compute_policy_rename(f, target_folder)
                        if pol_renamed:
                            info["target_filename"] = pol_renamed
                            info["is_confirmed"] = True
                            info["user_confirmed"] = True
                elif used_rule_routing and matched_rule:
                    info["routed_by"] = matched_rule[0]
                    info["match"] = matched_rule[1]
                else:
                    info["routed_by"] = "historical"
                    info["match"] = "user assignment"

                if is_overridden_lock:
                    info["is_corrected"] = True
                    info["corrected"] = True
                    info["is_overridden"] = True
                    info["overridden"] = True
                    info["original_lock_path"] = original_lock_path
                    info["original_path"] = original_lock_path
                    info["user_lock_path"] = original_lock_path
                    info["historical_path"] = original_lock_path
                    info["policy_path"] = compliance_path
                    info["new_policy_path"] = compliance_path
                    info["is_conflicted"] = True
                    info["compliance_path"] = compliance_path
                elif is_conflicted:
                    info["is_conflicted"] = True
                    info["compliance_path"] = compliance_path
                    info["historical_path"] = original_hist_path

                plan[target_folder][f] = info

            # Phase 3: Route unsupported files safely (skip files already routed into plan)
            if unsupported_files:
                routed_in_plan: set[str] = set()

                def _collect_routed_keys(node: dict[str, Any]) -> None:
                    for rk, rv in node.items():
                        if _is_file_leaf(rv):
                            routed_in_plan.add(str(rk))
                            routed_in_plan.add(str(rk).replace("\\", "/"))
                        elif isinstance(rv, dict):
                            _collect_routed_keys(rv)

                _collect_routed_keys(plan)
                for f, status in unsupported_files:
                    norm_uf = str(f).replace("\\", "/")
                    if f in routed_in_plan or norm_uf in routed_in_plan:
                        continue
                    if "Miscellaneous" not in plan:
                        plan["Miscellaneous"] = {}
                    if f not in plan["Miscellaneous"]:
                        plan["Miscellaneous"][f] = {
                            "__type__": "file",
                            "routed_by": "fallback",
                            "match": "none",
                            "status": status,
                        }

            clean_plan = {}
            import ntpath

            for target_folder, files in plan.items():
                if cancel_check and cancel_check():
                    return SortingPlan()
                if not isinstance(files, dict) or not files:
                    continue

                if not target_folder:
                    for f, info in files.items():
                        clean_plan[f] = info
                    continue

                if os.path.isabs(target_folder) or ntpath.isabs(target_folder):
                    if target_folder not in clean_plan:
                        clean_plan[target_folder] = {}
                    for f, info in files.items():
                        clean_plan[target_folder][f] = info
                    continue

                norm_target_folder = target_folder.replace("\\", "/").strip("/")
                if not norm_target_folder or norm_target_folder == ".":
                    for f, info in files.items():
                        clean_plan[f] = info
                    continue

                parts = [p for p in norm_target_folder.split("/") if p]
                current = clean_plan
                for i, part in enumerate(parts):
                    if part not in current:
                        current[part] = {}
                    if not isinstance(current[part], dict):
                        current[part] = {"_original": current[part]}
                    if i == len(parts) - 1:
                        for f, info in files.items():
                            current[part][f] = info
                    else:
                        current = current[part]

            if runtime_settings and getattr(
                runtime_settings, "PRESERVE_HIERARCHY", False
            ):
                clean_plan = self._inject_hierarchy(clean_plan)

            if verified_target_names:
                from app.core.path_utils import _disambiguate_key

                matched_vt_keys: set[str] = set()

                def _apply_verified_target_names(curr_node: dict[str, Any]) -> None:
                    for k, v in list(curr_node.items()):
                        is_leaf_none = v is None
                        is_leaf_file = (
                            isinstance(v, dict) and v.get("__type__") == "file"
                        )
                        if is_leaf_none or is_leaf_file:
                            if isinstance(v, dict):
                                raw_sp = str(v.get("source_path") or k)
                                if "::" in raw_sp:
                                    s_root_v, raw_rel_v = raw_sp.split("::", 1)
                                else:
                                    s_root_v = str(
                                        v.get("source_root")
                                        or (
                                            k.split("::", 1)[0]
                                            if "::" in k
                                            else base_dir
                                        )
                                    )
                                    raw_rel_v = raw_sp
                            else:
                                if "::" in k:
                                    s_root_v, raw_rel_v = k.split("::", 1)
                                else:
                                    s_root_v = base_dir
                                    raw_rel_v = k
                            norm_rel_v = raw_rel_v.replace("\\", "/")
                            cand_keys = (
                                k,
                                k.replace("\\", "/"),
                                f"{s_root_v}::{norm_rel_v}" if s_root_v else norm_rel_v,
                                norm_rel_v,
                                raw_rel_v,
                            )
                            vt_name = None
                            for ck in cand_keys:
                                if ck in verified_target_names:
                                    vt_name = verified_target_names[ck]
                                    matched_vt_keys.add(ck)
                                    matched_vt_keys.add(norm_rel_v)
                                    if s_root_v:
                                        matched_vt_keys.add(f"{s_root_v}::{norm_rel_v}")
                                    break
                            if not vt_name:
                                continue
                            if is_leaf_none:
                                leaf_meta: dict[str, Any] = {
                                    "__type__": "file",
                                    "routed_by": "historical",
                                    "match": "user assignment",
                                    "status": None,
                                }
                                v = leaf_meta
                                curr_node[k] = v
                            assert isinstance(v, dict)
                            if v.get("policy_action") in (
                                "trash",
                                "quarantine",
                                "unarchive",
                            ):
                                continue
                            v["source_path"] = norm_rel_v
                            v["source_root"] = s_root_v or base_dir
                            dest_key = vt_name
                            if dest_key in curr_node and curr_node[dest_key] is not v:
                                dest_key = _disambiguate_key(
                                    curr_node, dest_key, is_file=True
                                )
                            if dest_key != k:
                                curr_node.pop(k, None)
                                curr_node[dest_key] = v
                            v["target_filename"] = dest_key
                            v["confirmed"] = True
                            v["is_confirmed"] = True
                            v["user_confirmed"] = True
                            v["is_locked"] = True
                        elif isinstance(v, dict) and v.get("__type__") != "directory":
                            _apply_verified_target_names(v)

                _apply_verified_target_names(clean_plan)

                for d_row in docs or ():
                    if len(d_row) > 3 and d_row[3]:
                        continue
                    doc_key_raw = str(d_row[0])
                    norm_doc_key = doc_key_raw.replace("\\", "/")
                    if (
                        doc_key_raw in matched_vt_keys
                        or norm_doc_key in matched_vt_keys
                    ):
                        continue
                    s_root_u, rel_u = parse_item_identifier(doc_key_raw, base_dir)
                    norm_rel_u = rel_u.replace("\\", "/")
                    if norm_rel_u in matched_vt_keys or "/" in norm_rel_u:
                        continue
                    vt_root_name = (
                        verified_target_names.get(doc_key_raw)
                        or verified_target_names.get(norm_doc_key)
                        or verified_target_names.get(norm_rel_u)
                    )
                    if not vt_root_name:
                        continue
                    dest_key = vt_root_name
                    if dest_key in clean_plan:
                        dest_key = _disambiguate_key(clean_plan, dest_key, is_file=True)
                    clean_plan[dest_key] = {
                        "__type__": "file",
                        "source_path": norm_rel_u,
                        "source_root": s_root_u or base_dir,
                        "routed_by": "historical",
                        "match": "user assignment",
                        "status": None,
                        "target_filename": dest_key,
                        "confirmed": True,
                        "is_confirmed": True,
                        "user_confirmed": True,
                        "is_locked": True,
                    }

            configured_dup_action = (
                str(getattr(runtime_settings, "DUPLICATE_ACTION", "off") or "off")
                .strip()
                .lower()
                if runtime_settings
                else "off"
            )

            # Post-process: ensure every file node in the plan has source_path and relative_source,
            # and prune no-op leaves where source already equals destination (unless slated for
            # trash/duplicate staging or active duplicate review).
            eff_dest_root = dest_dir or base_dir

            def _is_noop_destination(
                s_root: str,
                src_rel: str,
                dst_folder: str,
                raw_key: str,
                target_filename: str | None = None,
            ) -> bool:
                src_abs = os.path.normcase(
                    os.path.normpath(os.path.abspath(os.path.join(s_root, src_rel)))
                )
                if os.path.isabs(dst_folder) or ntpath.isabs(dst_folder):
                    dst_dir_abs = dst_folder
                elif dst_folder:
                    dst_dir_abs = os.path.join(eff_dest_root, dst_folder)
                else:
                    dst_dir_abs = eff_dest_root
                dst_fn = os.path.basename(
                    str(target_filename or raw_key).replace("\\", "/")
                )
                dst_abs = os.path.normcase(
                    os.path.normpath(os.path.abspath(os.path.join(dst_dir_abs, dst_fn)))
                )
                return src_abs == dst_abs

            def _ensure_relative_source(node, current_path=""):
                for k, v in list(node.items()):
                    if v is None:
                        src_root = base_dir
                        raw_k = k
                        if "::" in k:
                            src_root, raw_k = k.split("::", 1)
                        cand_abs = os.path.normpath(
                            os.path.abspath(os.path.join(src_root, raw_k))
                        )
                        keep_for_dup = (
                            cand_abs in primary_by_abs
                            and configured_dup_action in ("stage", "trash", "review")
                        )
                        if not keep_for_dup and _is_noop_destination(
                            src_root, raw_k, current_path, raw_k
                        ):
                            del node[k]
                            continue
                        rel_src = safe_relpath(
                            os.path.join(src_root, raw_k),
                            os.path.join(src_root, current_path)
                            if current_path
                            else src_root,
                        ).replace("\\", "/")
                        leaf_dict: dict[str, Any] = {
                            "__type__": "file",
                            "source_path": raw_k,
                            "relative_source": rel_src,
                            "source_root": src_root,
                            "routed_by": "clustering",
                            "status": None,
                        }
                        leaf_date = resolved_dates_map.get(k) or resolve_disk_file_date(
                            os.path.join(src_root, raw_k)
                            if src_root and not os.path.isabs(raw_k)
                            else raw_k,
                            base_dir=src_root,
                        )
                        if leaf_date:
                            leaf_dict["file_date"] = leaf_date
                        node[k] = leaf_dict
                    elif isinstance(v, dict):
                        if v.get("__type__") == "file":
                            if not v.get("routed_by"):
                                v["routed_by"] = "clustering"
                            raw_k = k.split("::", 1)[1] if "::" in k else k
                            if "::" in k and "source_root" not in v:
                                s_root, _ = k.split("::", 1)
                                v["source_root"] = s_root
                            elif "source_root" not in v:
                                v["source_root"] = base_dir

                            if not v.get("source_path"):
                                v["source_path"] = raw_k
                            elif "::" in str(v.get("source_path", "")):
                                sp_root, sp_rel = str(v["source_path"]).split("::", 1)
                                v.setdefault("source_root", sp_root)
                                v["source_path"] = sp_rel

                            src_rel_for_calc = v.get("source_path") or raw_k
                            s_root = v.get("source_root") or base_dir
                            cand_abs = os.path.normpath(
                                os.path.abspath(
                                    src_rel_for_calc
                                    if os.path.isabs(str(src_rel_for_calc))
                                    else os.path.join(s_root, str(src_rel_for_calc))
                                )
                            )
                            keep_for_disposal = (
                                bool(v.get("policy_action"))
                                or v.get("duplicate_action") in ("stage", "trash")
                                or (
                                    cand_abs in primary_by_abs
                                    and configured_dup_action
                                    in ("stage", "trash", "review")
                                )
                            )
                            if not keep_for_disposal and _is_noop_destination(
                                s_root,
                                src_rel_for_calc,
                                current_path,
                                raw_k,
                                target_filename=v.get("target_filename"),
                            ):
                                del node[k]
                                continue
                            rel_src = safe_relpath(
                                os.path.join(s_root, src_rel_for_calc),
                                os.path.join(s_root, current_path)
                                if current_path
                                else s_root,
                            ).replace("\\", "/")
                            v["relative_source"] = rel_src
                            if not v.get("file_date"):
                                disk_cand = (
                                    os.path.join(s_root, src_rel_for_calc)
                                    if s_root and not os.path.isabs(src_rel_for_calc)
                                    else src_rel_for_calc
                                )
                                leaf_date = resolved_dates_map.get(
                                    k
                                ) or resolve_disk_file_date(disk_cand, base_dir=s_root)
                                if leaf_date:
                                    v["file_date"] = leaf_date
                        else:
                            # It's a directory, recurse
                            _ensure_relative_source(
                                v, os.path.join(current_path, k) if current_path else k
                            )
                            if not v:
                                del node[k]

            _ensure_relative_source(clean_plan)

            def _tag_byte_duplicates(node: dict[str, Any]) -> None:
                for k, v in list(node.items()):
                    if not isinstance(v, dict):
                        continue
                    if v.get("__type__") == "file":
                        s_root = v.get("source_root") or base_dir
                        src_rel = v.get("source_path") or (
                            k.split("::", 1)[1] if "::" in k else k
                        )
                        abs_src = os.path.normpath(
                            os.path.abspath(
                                src_rel
                                if os.path.isabs(str(src_rel))
                                else os.path.join(s_root, str(src_rel))
                            )
                        )
                        if abs_src in primary_group_map:
                            v["is_duplicate"] = False
                            v["is_duplicate_primary"] = True
                            v["duplicate_group_id"] = primary_group_map[abs_src]
                        elif abs_src in primary_by_abs:
                            orig_abs, gid = primary_by_abs[abs_src]
                            orig_key = abs_to_doc_key.get(orig_abs) or safe_relpath(
                                orig_abs, base_dir
                            )
                            v["is_duplicate"] = True
                            v["is_duplicate_primary"] = False
                            v["duplicate_of"] = orig_key
                            v["duplicate_group_id"] = gid
                            if v.get("policy_action") == "trash":
                                v["duplicate_action"] = "trash"
                                v["duplicate_resolved"] = True
                            elif configured_dup_action == "skip":
                                v["duplicate_action"] = "skip"
                                v["duplicate_resolved"] = True
                                v["is_excluded"] = True
                                v["is_checked"] = False
                                v["status"] = "Skipped"
                            elif configured_dup_action in ("stage", "trash"):
                                v["duplicate_action"] = configured_dup_action
                                v["duplicate_resolved"] = True
                            elif configured_dup_action == "review":
                                v["duplicate_resolved"] = False
                    else:
                        _tag_byte_duplicates(v)

            if primary_group_map or primary_by_abs:
                _tag_byte_duplicates(clean_plan)

            if runtime_settings and (
                getattr(runtime_settings, "CONTEXTUAL_RENAMING", False)
                or getattr(runtime_settings, "CLINICAL_SMART_RENAMING", False)
            ):
                try:
                    import gc

                    from app.core.file_renamer import FileRenamerEngine

                    renamer = FileRenamerEngine(
                        runtime_settings=runtime_settings,
                        db=self.db,
                        embedding_manager=getattr(self, "embedding_manager", None),
                    )
                    docs_map = (
                        {d[0]: d[1] for d in docs if len(d) > 1 and d[1]}
                        if docs
                        else {}
                    )
                    renamer.process_sorting_plan(
                        clean_plan,
                        documents_map=docs_map,
                        base_dir=base_dir,
                        locked_files=locked_files,
                        stop_words=self.stop_words,
                    )
                    docs_map.clear()
                    del docs_map
                    gc.collect()
                except Exception as e:
                    logging.error(
                        f"Contextual file renaming phase failed: {e}", exc_info=True
                    )

            validated_plan = _validate_sorting_plan_nodes(clean_plan)
            return self._build_sorting_plan(validated_plan, invalid_locks=invalid_locks)

        except Exception as e:
            logging.error(
                f"Failed during generate_sorting_plan. Error: {str(e)}", exc_info=True
            )
            return self._build_sorting_plan({}, invalid_locks=invalid_locks)
        finally:
            try:
                active_strategy_name = self.strategy_name
                if (
                    self.strategy_name
                    and self.strategy_name != "generative"
                    and getattr(self, "embedding_manager", None)
                    and self.embedding_manager.is_mock
                ):
                    active_strategy_name = "default"
                if active_strategy_name:
                    import app.core.analyzer_strategies

                    strategy = (
                        app.core.analyzer_strategies.clustering_registry.get_strategy(
                            active_strategy_name
                        )
                    )
                    if strategy and hasattr(strategy, "clear_isolated_state"):
                        cast(Any, strategy).clear_isolated_state()
            except Exception:
                logging.debug("Clearing isolated strategy state failed", exc_info=True)
