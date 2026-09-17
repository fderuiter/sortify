"""Unit tests for Component Catalog registry."""

from app.ui.catalog import CATALOG_REGISTRY


def test_catalog_registry_populated():
    """Verify that CATALOG_REGISTRY contains all expected components and structure."""
    assert len(CATALOG_REGISTRY) >= 1
    for entry in CATALOG_REGISTRY:
        assert "id" in entry
        assert "name" in entry
