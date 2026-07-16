# Architecture & Internals

## Data Flow: Directory Selection to Sorting Plan

The system relies on an automated, pipelined data flow from the moment the user selects a directory to the generation of the final sorting plan.

```mermaid
graph TD
    A[Directory Selection] --> B[File Extraction & Generator]
    B --> C[Chunked Yielding]
    C --> D[Incremental Analyzer (partial_fit)]
    D --> E[SentenceTransformers & KMeans Clustering]
    E --> F[Recursive Topic Grouping]
    F --> G[Generate Sorting Plan]
    G --> H[UI Tree Rendering]
```

### 1. Data Extraction
When a directory is selected, `build_corpus_generator` scans and extracts text from supported files (PDFs, DOCX, CSV, Excel, TXT).

### 2. ML Clustering & Recursive Analysis

See [IncrementalAnalyzer][app.core.analyzer.IncrementalAnalyzer] for details on the core incremental ingestion logic.

See [RecursiveKMeansStrategy][app.core.analyzer_strategies.RecursiveKMeansStrategy] for details on the hierarchical clustering approach used for deep folder structures.

### 3. Folder Naming Logic
Folder names are generated dynamically using KMeans components. The folder naming logic selects the top 2 terms for each topic and concatenates them with a hyphen (e.g., `Finance-Money`). Words are capitalized based on a TF-IDF vectorizer of the cluster documents.

## Threading Model & UI Responsiveness

The application is built using `customtkinter` and leverages threading to maintain a responsive user interface during heavy ML tasks.

- **Background Workers:** File scanning and incremental modeling run on a background thread (`pipeline_worker`).
- **Mutual Exclusion Locks:** A `threading.Lock` (`_update_lock`) is used when updating the ML model due to a manual drag-and-drop file move. This prevents concurrent model modifications that could corrupt the sorting plan.
- **Debouncing Timers:** The UI uses a debouncing mechanism (`threading.Timer`) set to 0.5 seconds when a user moves a file manually. This delays the recalculation of the clustering logic until the user finishes interacting, preventing UI freezes and redundant computations.
