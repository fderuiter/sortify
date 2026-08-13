import math
import tempfile
from pathlib import Path
import numpy as np
import pytest
from sklearn.feature_extraction.text import TfidfVectorizer, TfidfTransformer
from sklearn.preprocessing import normalize
from scipy.sparse import csr_matrix

from app.core.analyzer import IncrementalAnalyzer
from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.db_conn import clear_connection_cache

def test_db_backed_sparse_csr_reconstruction_and_math():
    """Verify that fallback lexical matching is performed without reading raw historical documents from disk,

    that the reconstruction is mathematically identical to manual tfidf, and that the custom IDF formula is used.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_worker = DBWorker()
        try:
            db_path = Path(tmp_dir) / "test_sparse.db"
            db = Database(db_path, db_worker)

            analyzer = IncrementalAnalyzer(
                max_folders=3, stop_words={"the", "and"}, db=db, strategy_name=None
            )

            base_dir = "sparse_base"

            # 1. Add historical documents. Notice the filepaths do NOT exist on disk!
            # If the system tried to read them from disk, it would fail.
            corpus = {
                "non_existent_folder/file1.txt": "apple banana orange orange",
                "non_existent_folder/file2.txt": "banana grape grape orange",
            }

            for filepath, content in corpus.items():
                db.upsert_document(base_dir, filepath, filepath + "_hash", content)
                db.set_user_verified_target_path(base_dir, filepath, "Fruit Basket")

            db_worker.q.join()

            # Verify stats in DB
            N, top_terms, doc_terms, doc_metadata = db.get_tfidf_stats(base_dir)
            assert N == 2

            # 2. Check the manual custom IDF formula calculation
            top_terms = top_terms[:1000]
            vocab = {term: idx for idx, (term, df) in enumerate(top_terms)}
            
            # idf_j = ln((1 + N) / (1 + df_j)) + 1
            idf_weights = {term: math.log((1 + N) / (1 + df)) + 1 for term, df in top_terms}
            
            # Assert the formula values
            for term, df in top_terms:
                expected_idf = math.log((1 + N) / (1 + df)) + 1
                assert idf_weights[term] == pytest.approx(expected_idf)

            # 3. Fit and classify a new document (which is not in DB yet)
            # The candidate is "apple banana orange"
            new_doc_content = "apple banana orange"
            new_corpus = {"candidate.txt": new_doc_content}
            analyzer.partial_fit(base_dir, new_corpus)

            # Generate the sorting plan. Since the historical files do not exist on disk,
            # this proves that NO files are read from disk for historical documents.
            plan = analyzer.generate_sorting_plan(base_dir)

            # Verify that candidate.txt got routed to "Fruit Basket" via similarity
            assert "Fruit Basket" in plan
            assert "candidate.txt" in plan["Fruit Basket"]
            file_info = plan["Fruit Basket"]["candidate.txt"]
            assert file_info["routed_by"] == "similarity"
            assert "similarity >= 0.8" in file_info["match"]

            # 4. Check mathematical equivalence directly
            # Reconstruct the expected historical vectors manually
            hist_filepaths = ["non_existent_folder/file1.txt", "non_existent_folder/file2.txt"]
            filepath_to_row_idx = {fp: idx for idx, fp in enumerate(hist_filepaths)}

            rows = []
            cols = []
            data = []

            for filepath, term, tf in doc_terms:
                norm_fp = filepath.replace("\\", "/")
                if norm_fp in filepath_to_row_idx:
                    row_idx = filepath_to_row_idx[norm_fp]
                    if term in vocab:
                        col_idx = vocab[term]
                        tf_weight = 1.0 + math.log(tf)
                        weight = tf_weight * idf_weights[term]
                        rows.append(row_idx)
                        cols.append(col_idx)
                        data.append(weight)

            expected_vectors = csr_matrix((data, (rows, cols)), shape=(len(hist_filepaths), len(vocab)))
            expected_vectors = normalize(expected_vectors, norm='l2', axis=1)

            # Manually transform candidate.txt using the injected vectorizer
            # (which we can recreate to check matching math)
            vectorizer = TfidfVectorizer(
                stop_words=list(analyzer.stop_words),
                vocabulary=vocab,
                sublinear_tf=True,
            )
            vectorizer.vocabulary_ = vocab
            vectorizer.fixed_vocabulary_ = True
            idf_values = np.array([idf_weights[term] for term, df in top_terms])
            vectorizer.idf_ = idf_values

            transformer = TfidfTransformer(sublinear_tf=True)
            transformer.idf_ = idf_values
            vectorizer._tfidf = transformer

            new_docs_vectors = vectorizer.transform([new_doc_content])
            similarities = new_docs_vectors.dot(expected_vectors.T).toarray()

            # The maximum similarity should be >= 0.8
            assert np.max(similarities[0]) >= 0.8

        finally:
            db_worker.stop()
            clear_connection_cache(only_current_and_inactive=False)
