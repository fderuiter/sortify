import os
import random

import pandas as pd
from docx import Document

CORPUS_DIR = "tests/corpus"
LARGE_CORPUS_DIR = "tests/large_corpus"


def create_corpus():
    os.makedirs(CORPUS_DIR, exist_ok=True)

    # 1. Plain text files
    with open(os.path.join(CORPUS_DIR, "finance_report.txt"), "w") as f:
        f.write(
            "This is a detailed report on finance, money, investment, and banking strategies."
        )

    with open(os.path.join(CORPUS_DIR, "tech_notes.txt"), "w") as f:
        f.write(
            "Notes on software engineering, computer science, algorithms, and technology."
        )

    with open(os.path.join(CORPUS_DIR, "empty.txt"), "w") as f:
        f.write("")

    # 2. Word document
    doc = Document()
    doc.add_paragraph(
        "Medical science, healthcare, doctor, patient, and clinical trials."
    )
    doc.save(os.path.join(CORPUS_DIR, "health_doc.docx"))

    # 3. CSV file
    df_csv = pd.DataFrame(
        {
            "Category": ["Finance", "Finance"],
            "Keyword": ["money", "banking"],
            "Description": ["investment strategies", "stock market trends"],
        }
    )
    df_csv.to_csv(os.path.join(CORPUS_DIR, "finance_data.csv"), index=False)

    # 4. Excel file
    df_excel = pd.DataFrame(
        {
            "Topic": ["Technology", "Technology"],
            "Subtopic": ["software", "hardware"],
            "Details": ["computer programming", "microchips and processors"],
        }
    )
    df_excel.to_excel(os.path.join(CORPUS_DIR, "tech_data.xlsx"), index=False)

    # 5. Real PDF file using reportlab
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(os.path.join(CORPUS_DIR, "science_doc.pdf"))
    c.drawString(
        100, 750, "Science, physics, chemistry, biology, and laboratory experiments."
    )
    c.save()


def create_large_corpus(num_docs=500):
    """Generate a large synthetic dataset with distinct semantic themes."""
    os.makedirs(LARGE_CORPUS_DIR, exist_ok=True)

    categories = {
        "Health": [
            "medical",
            "science",
            "healthcare",
            "doctor",
            "patient",
            "clinical",
            "hospital",
            "medicine",
            "treatment",
            "disease",
            "wellness",
            "therapy",
            "surgery",
        ],
        "Finance": [
            "finance",
            "money",
            "investment",
            "banking",
            "market",
            "stock",
            "economy",
            "trade",
            "capital",
            "revenue",
            "profit",
            "loss",
            "assets",
            "bonds",
        ],
        "Tech": [
            "software",
            "engineering",
            "computer",
            "algorithm",
            "technology",
            "hardware",
            "microchips",
            "data",
            "network",
            "code",
            "programming",
            "cloud",
            "security",
            "ai",
        ],
        "Science": [
            "physics",
            "chemistry",
            "biology",
            "laboratory",
            "experiment",
            "research",
            "molecule",
            "quantum",
            "astronomy",
            "genetics",
            "theory",
            "discovery",
        ],
    }

    # Generate deterministic content for reproducibility
    rng = random.Random(42)

    cat_names = list(categories.keys())
    for i in range(num_docs):
        cat = cat_names[i % len(cat_names)]
        words = rng.choices(categories[cat], k=rng.randint(15, 30))
        content = " ".join(words)

        filename = f"{cat.lower()}_doc_{i}.txt"
        with open(os.path.join(LARGE_CORPUS_DIR, filename), "w") as f:
            f.write(content.capitalize() + ".")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--large":
        num = int(sys.argv[2]) if len(sys.argv) > 2 else 500
        create_large_corpus(num)
    else:
        create_corpus()
