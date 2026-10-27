"""Shared sample data generation engine for Smart AutoSorter AI Pro."""

import os
from typing import Any, Dict, List, Optional

SAMPLE_CORPUS_ITEMS: Dict[str, str] = {
    "demo_finance.txt": (
        "This is a detailed report on finance, money, investment, and banking strategies. "
        "The economy is growing."
    ),
    "demo_tech.txt": (
        "Notes on software engineering, computer science, algorithms, and technology. "
        "Python is great."
    ),
    "demo_health.txt": (
        "Medical science, healthcare, doctor, patient, clinical trials, medicine, and health."
    ),
    "empty.txt": "",
}


def get_sample_corpus_items() -> Dict[str, str]:
    """Return dictionary of sample corpus file names mapped to their text content.

    Returns
    -------
        Dict[str, str]: Mapping of filename to text content.
    """
    return dict(SAMPLE_CORPUS_ITEMS)


def simulate_corpus_classification(
    selected_packs: Optional[List[str]] = None,
    settings: Optional[Any] = None,
) -> Dict[str, Dict[str, Any]]:
    """Run an in-memory classification dry-run against the sample document corpus.

    Evaluates keyword rules and compliance policies from selected starter template
    packs and/or application settings without writing any files to disk.

    Args:
        selected_packs: Optional list of starter template pack IDs to evaluate.
        settings: Optional application settings object.

    Returns
    -------
        Dict[str, Dict[str, Any]]: Mapping of sample document filename to its
        predicted classification result dictionary containing:
        - filename: str
        - content: str
        - category: str
        - target_path: str
        - confidence: float
        - matched_rule: str
    """
    from app.core.policy_engine import PolicyEngine
    from app.core.rule_templates import get_starter_template_by_id

    keyword_rules: Dict[str, str] = {}
    policies: List[Dict[str, Any]] = []

    # Load base rules from settings if provided
    if settings is not None:
        if hasattr(settings, "KEYWORD_RULES") and settings.KEYWORD_RULES:
            keyword_rules.update(settings.KEYWORD_RULES)
        if hasattr(settings, "POLICIES") and settings.POLICIES:
            policies.extend([dict(p) for p in settings.POLICIES])

    # Merge rules from selected starter template packs
    if selected_packs:
        for pack_id in selected_packs:
            pack = get_starter_template_by_id(pack_id)
            if pack:
                for kw, path in pack.keyword_rules.items():
                    if kw not in keyword_rules:
                        keyword_rules[kw] = path
                for policy in pack.policies:
                    if policy not in policies:
                        policies.append(dict(policy))

    sorted_policies = PolicyEngine.sort_policies(policies)
    sample_items = get_sample_corpus_items()
    results: Dict[str, Dict[str, Any]] = {}

    for filename, content in sample_items.items():
        matched_rule = "None"
        category = "Uncategorized"
        target_path = f"Uncategorized/{filename}"
        confidence = 0.0

        # 1. Evaluate compliance policies (highest priority)
        matched_policy = None
        for policy in sorted_policies:
            if PolicyEngine.match_policy(policy, filename, content, None):
                matched_policy = policy
                break

        if matched_policy:
            raw_target = str(matched_policy.get("target_path", "Compliance"))
            category = raw_target.split("/")[0] if "/" in raw_target else raw_target
            target_path = (
                raw_target
                if raw_target.endswith(filename)
                else os.path.join(raw_target, filename)
            )
            expr = matched_policy.get("expression", "policy")
            matched_rule = f"Policy ({expr})"
            confidence = 0.95
        else:
            # 2. Evaluate keyword rules
            matched_kw = None
            matched_folder = None
            content_lower = content.lower() if content else ""
            filename_lower = filename.lower()

            for kw, folder in keyword_rules.items():
                kw_lower = str(kw).lower()
                if kw_lower and (
                    kw_lower in filename_lower or kw_lower in content_lower
                ):
                    matched_kw = kw
                    matched_folder = folder
                    break

            if matched_kw and matched_folder:
                category = (
                    matched_folder.split("/")[0]
                    if "/" in matched_folder
                    else matched_folder
                )
                target_path = (
                    matched_folder
                    if matched_folder.endswith(filename)
                    else os.path.join(matched_folder, filename)
                )
                matched_rule = f"Keyword ({matched_kw})"
                confidence = 0.88
            else:
                # 3. Fallback semantic heuristics
                if (
                    "finance" in filename_lower
                    or "finance" in content_lower
                    or "money" in content_lower
                ):
                    category = "Financial"
                    target_path = f"Financial/Invoices/{filename}"
                    confidence = 0.75
                    matched_rule = "Semantic (finance)"
                elif (
                    "tech" in filename_lower
                    or "software" in content_lower
                    or "computer" in content_lower
                ):
                    category = "Technology"
                    target_path = f"Technology/Notes/{filename}"
                    confidence = 0.75
                    matched_rule = "Semantic (technology)"
                elif (
                    "health" in filename_lower
                    or "medical" in content_lower
                    or "doctor" in content_lower
                ):
                    category = "Medical"
                    target_path = f"Medical/Records/{filename}"
                    confidence = 0.75
                    matched_rule = "Semantic (medical)"

        results[filename] = {
            "filename": filename,
            "content": content,
            "category": category,
            "target_path": target_path,
            "confidence": confidence,
            "matched_rule": matched_rule,
        }

    return results


def generate_sample_corpus(base_dir: str, overwrite: bool = False) -> List[str]:
    """Generate a sample corpus with categorized text documents (finance, tech, health, empty).

    Args:
        base_dir: Target directory path where sample documents will be created.
        overwrite: If False and any sample files exist in base_dir, raises FileExistsError.

    Returns
    -------
        List of created relative file names.
    """
    os.makedirs(base_dir, exist_ok=True)
    sample_files = get_sample_corpus_items()

    if not overwrite:
        existing = [
            f for f in sample_files if os.path.exists(os.path.join(base_dir, f))
        ]
        if existing:
            raise FileExistsError(
                f"Sample files already exist in {base_dir}: {', '.join(existing)}"
            )

    for filename, content in sample_files.items():
        filepath = os.path.join(base_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

    return list(sample_files.keys())
