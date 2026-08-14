import threading
import time
import pytest
from app.core.analyzer_strategies import clustering_registry, RecursiveKMeansStrategy, GenerativeNamingStrategy
from app.core.shared_registry import ContextPropagatingThreadPoolExecutor, ContextPropagatingThread


def test_strategy_singleton_retrieval_and_type():
    """Verify that strategies are retrieved from the registry as singletons and inherit from IsolatedStrategyMixin."""
    strategy1 = clustering_registry.get_strategy("default")
    strategy2 = clustering_registry.get_strategy("default")
    assert strategy1 is strategy2
    assert isinstance(strategy1, RecursiveKMeansStrategy)

    gen_strategy1 = clustering_registry.get_strategy("generative")
    gen_strategy2 = clustering_registry.get_strategy("generative")
    assert gen_strategy1 is gen_strategy2
    assert isinstance(gen_strategy1, GenerativeNamingStrategy)


def test_concurrent_strategy_execution_isolation():
    """Verify that concurrent strategy execution isolates parameters and does not cause cross-contamination."""
    strategy = clustering_registry.get_strategy("default")

    # Barrier to ensure both threads enter and run concurrently
    barrier = threading.Barrier(2)
    captured_states = {}

    original_get_cluster_keywords = strategy._get_cluster_keywords

    def mocked_get_cluster_keywords(documents):
        thread_name = threading.current_thread().name
        # Capture state while inside execution
        captured_states[thread_name] = {
            "stop_words": strategy.stop_words,
            "max_folders": strategy.max_folders,
            "max_depth": strategy.max_depth,
            "max_features": strategy.max_features,
        }
        # Wait for the other thread to reach this point too
        barrier.wait()
        return original_get_cluster_keywords(documents)

    # Patch the strategy instance method temporarily
    strategy._get_cluster_keywords = mocked_get_cluster_keywords

    filenames = ["f1.txt", "f2.txt", "f3.txt", "f4.txt"]
    documents = [
        "apple banana orange fruit salad",
        "apple banana orange fruit salad",
        "car bike truck vehicle garage",
        "car bike truck vehicle garage",
    ]

    def run_thread_1():
        # Thread 1 has unique stop_words and max_folders
        strategy.generate_plan(
            filenames,
            documents,
            max_folders=2,
            stop_words={"apple", "banana"},
            max_depth=3,
            max_features=2,
        )

    def run_thread_2():
        # Thread 2 has different unique stop_words and max_folders
        strategy.generate_plan(
            filenames,
            documents,
            max_folders=10,
            stop_words={"car", "bike"},
            max_depth=5,
            max_features=4,
        )

    t1 = threading.Thread(target=run_thread_1, name="Thread-1")
    t2 = threading.Thread(target=run_thread_2, name="Thread-2")

    try:
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)
    finally:
        # Restore original method
        strategy._get_cluster_keywords = original_get_cluster_keywords

    # Assert that both threads captured correct, distinct, isolated values during concurrent execution!
    assert "Thread-1" in captured_states
    assert "Thread-2" in captured_states

    assert captured_states["Thread-1"]["stop_words"] == {"apple", "banana"}
    assert captured_states["Thread-1"]["max_folders"] == 2
    assert captured_states["Thread-1"]["max_depth"] == 3
    assert captured_states["Thread-1"]["max_features"] == 2

    assert captured_states["Thread-2"]["stop_words"] == {"car", "bike"}
    assert captured_states["Thread-2"]["max_folders"] == 10
    assert captured_states["Thread-2"]["max_depth"] == 5
    assert captured_states["Thread-2"]["max_features"] == 4


def test_strategy_context_propagation_across_thread_pool():
    """Verify that isolated state/context propagates reliably across background thread pools using contextvars."""
    strategy = clustering_registry.get_strategy("default")

    # Set parameters on strategy in main thread
    strategy.db = "test_db"
    strategy.base_dir = "test_dir"

    captured_db = [None]
    captured_base_dir = [None]

    def background_task():
        # Verify the background thread pool task inherits/propagates the state correctly
        captured_db[0] = getattr(strategy, "db", None)
        captured_base_dir[0] = getattr(strategy, "base_dir", None)

    with ContextPropagatingThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(background_task)
        future.result()

    assert captured_db[0] == "test_db"
    assert captured_base_dir[0] == "test_dir"

    # Verify standard thread does NOT propagate unless it's a ContextPropagatingThread
    captured_non_prop = [None]
    def background_task_non_prop():
        try:
            captured_non_prop[0] = getattr(strategy, "db", None)
        except AttributeError:
            captured_non_prop[0] = "AttributeError raised"

    t = threading.Thread(target=background_task_non_prop)
    t.start()
    t.join()
    # It should not have been propagated because regular threading.Thread doesn't copy contextvars
    assert captured_non_prop[0] != "test_db"

    # Verify ContextPropagatingThread DOES propagate correctly
    captured_prop_thread = [None]
    def background_task_prop_thread():
        captured_prop_thread[0] = getattr(strategy, "db", None)

    pct = ContextPropagatingThread(target=background_task_prop_thread)
    pct.start()
    pct.join()
    assert captured_prop_thread[0] == "test_db"

    # Clean up state on main thread
    strategy.clear_isolated_state()


def test_strategy_automatic_cleanup():
    """Verify that thread-local cleanup occurs automatically when a background sorting job completes to prevent memory leaks."""
    strategy = clustering_registry.get_strategy("default")

    # Before calling generate_plan, isolated variables are not set
    with pytest.raises(AttributeError):
        _ = strategy.stop_words

    filenames = ["f1.txt", "f2.txt"]
    documents = ["doc one", "doc two"]

    # 1. Test cleanup of generate_plan when executed on a background thread
    def run_on_bg_thread():
        # This should execute successfully and perform cleanup at the end because it's a bg thread
        strategy.generate_plan(
            filenames,
            documents,
            max_folders=2,
            stop_words={"test"},
            max_depth=5,
            max_features=3,
        )
        # Since it runs on a background thread, the state should be automatically cleared on function completion
        assert not hasattr(strategy, "stop_words")
        assert not hasattr(strategy, "max_folders")

    t = threading.Thread(target=run_on_bg_thread)
    t.start()
    t.join()

    # 2. Test cleanup of generate_sorting_plan on main thread (the sorting job completes via finally block)
    # Let's mock a simple database and analyzer to test generate_sorting_plan cleanup
    from app.core.analyzer import IncrementalAnalyzer
    from unittest.mock import MagicMock

    mock_db = MagicMock()
    mock_db.get_all_documents.return_value = [
        ("f1.txt", "doc one", "h1", ""),
        ("f2.txt", "doc two", "h2", ""),
    ]

    analyzer = IncrementalAnalyzer(
        max_folders=2,
        stop_words={"test"},
        db=mock_db,
        strategy_name="default",
    )

    # Let's ensure the strategy is resolved and clear beforehand
    strategy.clear_isolated_state()
    with pytest.raises(AttributeError):
        _ = strategy.stop_words

    # Execute the high level sorting plan
    analyzer.generate_sorting_plan("test_dir")

    # The high level sorting plan should have automatically cleaned up the strategy at completion
    with pytest.raises(AttributeError):
        _ = strategy.stop_words
    with pytest.raises(AttributeError):
        _ = strategy.max_folders

