import os

import pandas as pd
from docx import Document

CORPUS_DIR = "tests/corpus"

def create_corpus():
    os.makedirs(CORPUS_DIR, exist_ok=True)
    
    # 1. Plain text files
    with open(os.path.join(CORPUS_DIR, "finance_report.txt"), "w") as f:
        f.write("This is a detailed report on finance, money, investment, and banking strategies.")
        
    with open(os.path.join(CORPUS_DIR, "tech_notes.txt"), "w") as f:
        f.write("Notes on software engineering, computer science, algorithms, and technology.")
        
    with open(os.path.join(CORPUS_DIR, "empty.txt"), "w") as f:
        f.write("")
        
    # 2. Word document
    doc = Document()
    doc.add_paragraph("Medical science, healthcare, doctor, patient, and clinical trials.")
    doc.save(os.path.join(CORPUS_DIR, "health_doc.docx"))
    
    # 3. CSV file
    df_csv = pd.DataFrame({
        "Category": ["Finance", "Finance"],
        "Keyword": ["money", "banking"],
        "Description": ["investment strategies", "stock market trends"]
    })
    df_csv.to_csv(os.path.join(CORPUS_DIR, "finance_data.csv"), index=False)
    
    # 4. Excel file
    df_excel = pd.DataFrame({
        "Topic": ["Technology", "Technology"],
        "Subtopic": ["software", "hardware"],
        "Details": ["computer programming", "microchips and processors"]
    })
    df_excel.to_excel(os.path.join(CORPUS_DIR, "tech_data.xlsx"), index=False)

    # 5. Real PDF file using reportlab
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(os.path.join(CORPUS_DIR, "science_doc.pdf"))
    c.drawString(100, 750, "Science, physics, chemistry, biology, and laboratory experiments.")
    c.save()

if __name__ == "__main__":
    create_corpus()
