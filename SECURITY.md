# Security Policy

## Security Posture

Smart AutoSorter AI Pro is designed with a strong focus on security and data locality. The application operates primarily offline and handles all data processing on your local machine to minimize attack vectors.

## Vulnerability Reporting

If you discover any security vulnerabilities in Smart AutoSorter AI Pro, please report them to our security team. You can submit a vulnerability report by opening an issue on our GitHub repository with the label `security`, or by emailing the project maintainers directly if an email address has been provided in the repository details. We are committed to addressing security concerns promptly and responsibly.

## Telemetry and Cloud Dependencies

We respect your privacy and security. Smart AutoSorter AI Pro explicitly confirms that there are **no telemetry, analytics, or cloud-based API reporting** mechanisms present in the application. We do not track your usage, and we do not send your data to any remote servers for analysis.

## Network Dependencies

While the application primarily operates offline, there is a **one-time network dependency** required during the initial setup. On the first run, the application will download pre-trained model weights (specifically `all-MiniLM-L6-v2` via the `sentence-transformers` library) from the Hugging Face Hub. This is strictly a download of static model files required for the semantic clustering features. Subsequent executions of the application will not require internet access unless the cached model is deleted.
