# Architecture & Internals

## Data Flow: Directory Selection to Sorting Plan

The system relies on an automated, pipelined data flow from the moment the user selects a directory to the generation of the final sorting plan.

```mermaid
graph TD
    A[Directory Selection] --> B[File Extraction & Generator]
    B --> C[Chunked Yielding]
    C --> D[Incremental Analyzer (partial_fit)]
    D --> E[MiniBatchNMF Clustering]
    E --> F[Recursive Topic Grouping]
    F --> G[Generate Sorting Plan]
    G --> H[UI Tree Rendering]
```

### 1. Data Extraction
When a directory is selected, `build_corpus_generator` scans and extracts text from supported files (PDFs, DOCX, CSV, Excel, TXT).

### 2. ML Clustering & Recursive Analysis
The `IncrementalAnalyzer` processes files using `HashingVectorizer` and `MiniBatchNMF`.

**Core ML Constraints:**
- **Minimum Documents:** A cluster requires a **minimum of 3 documents** to be split. If fewer documents are available, clustering terminates.
- **Recursion Depth:** The algorithm stops subdividing groups when a **5-level recursion limit** is reached. At the top level (depth 1), fallback files go to the "Miscellaneous" folder.
- **Word Constraints:** The system explicitly **ignores words with fewer than 3 characters** during vocabulary building and analysis.

### 3. Folder Naming Logic
Folder names are generated dynamically using `MiniBatchNMF` components. The folder naming logic selects the top 2 terms for each topic and concatenates them with a hyphen (e.g., `Finance-Money`). Words are capitalized based on a reverse lookup from the HashingVectorizer indices.

## Threading Model & UI Responsiveness

The application is built using `customtkinter` and leverages threading to maintain a responsive user interface during heavy ML tasks.

- **Background Workers:** File scanning and incremental modeling run on a background thread (`pipeline_worker`).
- **Mutual Exclusion Locks:** A `threading.Lock` (`_update_lock`) is used when updating the ML model due to a manual drag-and-drop file move. This prevents concurrent model modifications that could corrupt the sorting plan.
- **Debouncing Timers:** The UI uses a debouncing mechanism (`threading.Timer`) set to 0.5 seconds when a user moves a file manually. This delays the recalculation of the clustering logic until the user finishes interacting, preventing UI freezes and redundant computations.
