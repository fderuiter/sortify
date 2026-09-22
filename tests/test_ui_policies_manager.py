from app.ui.settings import get_shadowed_policies


def test_get_shadowed_policies():
    """Verify that get_shadowed_policies correctly identifies shadowed policies based on type and priority."""
    policies = [
        {
            "type": "keyword",
            "expression": "invoice",
            "target_path": "Invoices",
            "priority": 10,
        },
        {
            "type": "keyword",
            "expression": "invoice_overdue",
            "target_path": "Overdue",
            "priority": 5,
        },
        {
            "type": "keyword",
            "expression": "invoice_final",
            "target_path": "Final",
            "priority": 20,
        },
        {
            "type": "pattern",
            "expression": "final_invoice",
            "target_path": "FinalPattern",
            "priority": 5,
        },
    ]

    shadowed = get_shadowed_policies(policies)
    assert shadowed == [False, True, False, True]


def test_get_shadowed_policies_tie_breaking():
    """Verify stable tie-breaking when priorities are equal (preserving original list order)."""
    policies = [
        {
            "type": "keyword",
            "expression": "invoice",
            "target_path": "First",
            "priority": 10,
        },
        {
            "type": "keyword",
            "expression": "invoice_overdue",
            "target_path": "Second",
            "priority": 10,
        },
    ]
    shadowed = get_shadowed_policies(policies)
    assert shadowed == [False, True]
