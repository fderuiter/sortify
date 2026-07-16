# Contributor Guide

Welcome to the Smart AutoSorter AI Pro project! This guide will help you get started with testing and contributing.

## Development Setup

To set up your local development environment and sync dependencies, run the workspace setup task:

```bash
python tasks.py setup
```

Once the setup is complete, verify your environment by running the test suite:

```bash
python tasks.py test
```

## Interactive CLI Demo Mode

To help new developers quickly understand the end-to-end data flow without reviewing source code, the system includes an interactive demo mode.

### Running the Demo

The demo automatically generates a sample corpus of documents that meet the pipeline requirements (minimum of 3 documents). It then simulates the background extraction and clustering logic, ultimately printing the resulting sorting plan.

To run the interactive demo, use the following command:

```bash
uv run smart-autosorter --demo
```

### Automated Sample Corpus Utility

The demo mode leverages an internal automated utility (`generate_sample_corpus` in `app/demo.py`) that quickly builds a test dataset containing at least 3 documents. This satisfies the minimum document constraint required by the ML clustering engine. Developers can review this script to see how sample data (e.g., mock text files on finance and technology) is assembled for local testing.
