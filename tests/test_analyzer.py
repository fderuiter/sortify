from app.core.analyzer import IncrementalAnalyzer


def test_incremental_analyzer_init():
    analyzer = IncrementalAnalyzer(max_folders=5)
    assert analyzer.max_folders == 5
    assert analyzer.n_features == 10000
    assert analyzer.corpus == {}


def test_partial_fit():
    analyzer = IncrementalAnalyzer(max_folders=3)
    corpus = {"file1.txt": "This is a document about finance and money.", "file2.txt": "Science and technology are great."}
    
    analyzer.partial_fit(corpus)
    assert len(analyzer.corpus) == 2
    assert "file1.txt" in analyzer.corpus


def test_partial_fit_empty():
    analyzer = IncrementalAnalyzer(max_folders=3)
    analyzer.partial_fit({})
    assert len(analyzer.corpus) == 0


def test_generate_sorting_plan_empty():
    analyzer = IncrementalAnalyzer(max_folders=3)
    plan = analyzer.generate_sorting_plan()
    assert plan == {}


def test_generate_sorting_plan():
    analyzer = IncrementalAnalyzer(max_folders=2)
    corpus = {
        "finance1.txt": "money bank finance investment",
        "finance2.txt": "investment stock market money",
        "tech1.txt": "software computer science technology",
        "tech2.txt": "technology hardware computer"
    }
    analyzer.partial_fit(corpus)
    plan = analyzer.generate_sorting_plan()
    
    # Check that there are at least some folders created or files added
    assert isinstance(plan, dict)
    assert len(plan) > 0


def test_partial_fit_exception(mocker):
    analyzer = IncrementalAnalyzer(max_folders=2)
    mocker.patch.object(analyzer, "_update_vocab", side_effect=Exception("Test error"))
    mock_logger = mocker.patch("app.core.analyzer.logging.error")
    
    corpus = {"file.txt": "test content"}
    analyzer.partial_fit(corpus)
    
    mock_logger.assert_called_once()
    assert "file.txt" in analyzer.corpus  # Update still happened before exception


def test_generate_sorting_plan_exception(mocker):
    analyzer = IncrementalAnalyzer(max_folders=2)
    corpus = {"file.txt": "test content"}
    analyzer.partial_fit(corpus)
    
    mocker.patch.object(analyzer, "_cluster_recursive", side_effect=Exception("Test error"))
    mock_logger = mocker.patch("app.core.analyzer.logging.error")
    
    plan = analyzer.generate_sorting_plan()
    
    mock_logger.assert_called_once()
    assert plan == {}
