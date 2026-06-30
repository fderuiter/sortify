"""Core semantic analysis module.

This module provides topic modeling functionality.
"""

from collections import defaultdict

from config import MAX_DF, MIN_DF, STOP_WORDS
from sklearn.decomposition import NMF
from sklearn.feature_extraction.text import TfidfVectorizer


def generate_sorting_plan(corpus: dict, max_folders: int, log_callback=None) -> dict:
    """Generate a machine learning based sorting plan to cluster documents.

    Uses TF-IDF + NMF to cluster documents by connected themes and returns a
    sorting plan mapping the connected keywords defining each folder to lists
    of filenames.

    Parameters
    ----------
    corpus : dict
        A dictionary mapping filenames to their extracted text content.
    max_folders : int
        The maximum number of semantic folders to create.

    Returns
    -------
    dict
        A mapping where keys are generated folder names and values are lists
        of filenames belonging to that folder.

    """
    documents = list(corpus.values())
    filenames = list(corpus.keys())

    plan = defaultdict(list)

    # 1. Edge Case: If there are too few files, ML will fail. Fallback to basic sorting.
    if log_callback:
        log_callback("Analyzing topics using local TF-IDF Vectorizer...")
    if len(documents) < 3:
        for f in filenames:
            plan["Miscellaneous"].append(f)
        return plan

    # 2. Extract significant vocabulary while ignoring generic terms
    vectorizer = TfidfVectorizer(
        max_df=MAX_DF, min_df=MIN_DF, stop_words=list(STOP_WORDS)
    )
    try:
        tfidf_matrix = vectorizer.fit_transform(documents)
    except ValueError:
        # Happens if files contain no recognizable text at all
        for f in filenames:
            plan["Miscellaneous"].append(f)
        return plan

    # 3. Topic Modeling: Group files into semantic clusters
    if log_callback:
        log_callback("Running NMF algorithm locally for semantic clustering...")
    actual_k = min(max_folders, len(documents) // 2, tfidf_matrix.shape[1])
    if actual_k < 2:
        actual_k = 2

    nmf_model = NMF(n_components=actual_k, random_state=42)
    document_topic_matrix = nmf_model.fit_transform(tfidf_matrix)
    topic_word_matrix = nmf_model.components_

    feature_names = vectorizer.get_feature_names_out()

    # 4. Name folders based on the strongest connected terms
    folder_names = []
    for topic in topic_word_matrix:
        # Get top 2 connected terms to name the folder (e.g., "Invoice_Billing")
        top_indices = topic.argsort()[:-3:-1]
        top_terms = [feature_names[i].capitalize() for i in top_indices]
        folder_names.append("-".join(top_terms))

    # 5. Map each file to its dominant topic cluster
    for i, filename in enumerate(filenames):
        # Find which topic cluster this document scores highest in
        best_topic_idx = document_topic_matrix[i].argmax()

        # If the highest score is 0, the document had no matching text
        if document_topic_matrix[i][best_topic_idx] == 0:
            plan["Miscellaneous"].append(filename)
        else:
            folder = folder_names[best_topic_idx]
            plan[folder].append(filename)

    return dict(plan)
