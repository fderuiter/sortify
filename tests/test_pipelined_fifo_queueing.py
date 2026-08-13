import queue
import logging
from unittest.mock import MagicMock, patch
import pytest

from app.core.analyzer_strategies import GenerativeNamingStrategy


def test_pipelined_fifo_queueing_success():
    """Verify that all validation tasks are queued asynchronously before responses are awaited,

    and that FIFO responses are correctly aggregated and mapped back to original files.
    """
    strategy = GenerativeNamingStrategy()
    strategy._model_initialized = True
    strategy._gguf_active = True
    strategy._gguf_failed = False
    
    # Mock GGUF queues and process
    input_queue = queue.Queue()
    output_queue = queue.Queue()
    strategy._gguf_input_queue = input_queue
    strategy._gguf_output_queue = output_queue
    
    strategy._gguf_process = MagicMock()
    strategy._gguf_process.is_alive.return_value = True

    # Pre-populate the output queue with responses corresponding to the tasks
    # We expect 3 tasks: file1.txt, file2.txt, file3.txt
    output_queue.put({"text": "YES"})
    output_queue.put({"text": "NO"})
    output_queue.put({"text": "YES"})

    filenames = ["file1.txt", "file2.txt", "file3.txt"]
    documents = ["doc 1", "doc 2", "doc 3"]

    # Mock the superclass's generate_plan to return a plan structure
    mock_raw_plan = {
        "Folder A": {
            "file1.txt": None,
            "file2.txt": None,
        },
        "Folder B": {
            "file3.txt": None,
        }
    }

    with patch(
        "app.core.analyzer_strategies.RecursiveKMeansStrategy.generate_plan",
        return_value=(mock_raw_plan, 0.0)
    ):
        # Generate the plan
        new_plan, error = strategy.generate_plan(filenames, documents, 2, set())

        # Verify all tasks were pushed to input_queue before they were fetched
        assert input_queue.qsize() == 3
        
        # Verify the tasks inside input_queue
        task1 = input_queue.get()
        task2 = input_queue.get()
        task3 = input_queue.get()
        
        assert "doc 1" in task1["prompt"]
        assert "doc 2" in task2["prompt"]
        assert "doc 3" in task3["prompt"]

        # Verify that file2.txt went to 'Low Confidence' due to NO response,
        # while file1.txt and file3.txt remained in their folders.
        assert "Folder A" in new_plan
        assert "file1.txt" in new_plan["Folder A"]
        assert "file2.txt" not in new_plan["Folder A"]
        
        assert "Folder B" in new_plan
        assert "file3.txt" in new_plan["Folder B"]
        
        assert "Low Confidence" in new_plan
        assert "file2.txt" in new_plan["Low Confidence"]


def test_pipelined_fifo_queueing_timeout_handling():
    """Verify that a timeout on a task retrieval from the GGUF worker queue

    does not block the entire bulk retrieval, and falls back to synchronous execution gracefully.
    """
    strategy = GenerativeNamingStrategy()
    strategy._model_initialized = True
    strategy._gguf_active = True
    strategy._gguf_failed = False
    
    # Mock GGUF queues and process
    input_queue = queue.Queue()
    output_queue = queue.Queue()
    strategy._gguf_input_queue = input_queue
    strategy._gguf_output_queue = output_queue
    
    strategy._gguf_process = MagicMock()
    strategy._gguf_process.is_alive.return_value = True

    # Put only 1 response, causing the second task to time out
    output_queue.put({"text": "YES"})

    filenames = ["file1.txt", "file2.txt"]
    documents = ["doc 1", "doc 2"]

    mock_raw_plan = {
        "Folder A": {
            "file1.txt": None,
            "file2.txt": None,
        }
    }

    # Mock the fallback PyTorch/synchronous generation
    strategy._run_prompt = MagicMock(return_value="YES")

    with patch(
        "app.core.analyzer_strategies.RecursiveKMeansStrategy.generate_plan",
        return_value=(mock_raw_plan, 0.0)
    ):
        with patch(
            "app.core.analyzer_strategies.cooperative_queue_get",
            side_effect=[{"text": "YES"}, queue.Empty("Timeout error")]
        ) as mock_cooperative_get:
            
            with patch.object(strategy, "_fallback_to_pytorch") as mock_fallback:
                # Generate the plan
                new_plan, error = strategy.generate_plan(filenames, documents, 2, set())

                # It should have called fallback once GGUF failed/timed out
                mock_fallback.assert_called_once()
                
                # It should have called _run_prompt as a fallback for the second file
                strategy._run_prompt.assert_called_once()

                # Both files should be classified correctly
                assert "Folder A" in new_plan
                assert "file1.txt" in new_plan["Folder A"]
                assert "file2.txt" in new_plan["Folder A"]
