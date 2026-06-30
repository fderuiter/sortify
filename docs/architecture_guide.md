# Architectural Guide: Local Machine Learning Implementation

This document details the technical implementation of Smart AutoSorter AI Pro, emphasizing its local-only processing approach. 

## Overview
Unlike cloud-based document parsers that send data to external APIs, this application implements a completely local Natural Language Processing (NLP) pipeline.

## Scikit-Learn Stack Implementation

The core of our AI processing is built entirely around `scikit-learn`. The lack of external API dependencies guarantees data sovereignty for privacy-conscious users.

1. **Text Extraction:** Documents are parsed using standard local Python libraries (`pypdf`, `python-docx`, etc.) to produce a text corpus without leaving the machine.
2. **TF-IDF Vectorization:** We utilize `TfidfVectorizer` from `scikit-learn` to process the extracted text locally. It calculates term frequencies while ignoring standard English stop words.
3. **Semantic Clustering (NMF):** We apply Non-Negative Matrix Factorization (`NMF`) to group documents into distinct thematic folders. This algorithm runs entirely in-memory on the user's local CPU.
4. **Execution:** File moving operations are orchestrated locally using Python's standard `shutil` library, ensuring that neither the file content nor the file structures are transmitted over any network.

By avoiding cloud LLM endpoints (like OpenAI or AWS Comprehend), the application eliminates network latency and secures user data against external exposure.
