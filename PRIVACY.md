# Privacy Policy

## Local Data Processing

Smart AutoSorter AI Pro is built around a privacy-first architecture. All processing of your documents, including text extraction and semantic clustering, occurs entirely on your local machine.

We utilize local libraries to ensure your data is processed securely and efficiently:
- **SQLite:** Used for local data and cache storage.
- **scikit-learn:** Utilized for offline document clustering and thematic analysis.

## Telemetry and Data Sharing

Your data stays with you. We reiterate that **user data does not leave the local machine**. The application does not include any telemetry, tracking, or cloud-based analytics. Your documents, their contents, and the resulting organizational structure are never sent to external servers or third parties.

## Data Storage and Encryption

While we prioritize keeping your data on your local device, it is important to be aware of how the data is stored. We explicitly disclose that the **local storage for document text and embeddings is unencrypted**. This includes any caches stored in the local SQLite databases. If you are processing sensitive documents, we strongly recommend relying on full-disk encryption provided by your operating system (such as BitLocker for Windows or LUKS for Linux) to secure your data at rest.
