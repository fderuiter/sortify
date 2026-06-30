# Privacy Manifest

## Local Data Processing Guarantee
Smart AutoSorter AI Pro is designed with a privacy-first architecture. All document processing, text extraction, and semantic analysis occur entirely on the local machine. 
**We explicitly confirm the absence of external API keys, cloud service dependencies, or any network calls during the operation of this application.**

## Libraries Used

| Library | Role in Local Processing | Network Calls? |
|---------|---------------------------|----------------|
| `scikit-learn` | Powers the TF-IDF Vectorization and Non-Negative Matrix Factorization (NMF) for local semantic topic clustering. | No |
| `pandas` / `openpyxl` | Extracts text from Excel files and spreadsheets locally. | No |
| `python-docx` | Parses Word documents locally to extract raw text content. | No |
| `pypdf` | Extracts text from PDF files directly on the local machine. | No |
| `customtkinter` | Renders the modern graphical user interface and real-time privacy verification panel. | No |
