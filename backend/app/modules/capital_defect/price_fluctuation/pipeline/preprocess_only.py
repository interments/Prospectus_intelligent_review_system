from __future__ import annotations

import argparse
from pathlib import Path

from ..extractors.plumber_extractor import extract_pdf_with_pdfplumber, persist_preprocess_output


def main() -> None:
    parser = argparse.ArgumentParser(description="Preprocess prospectus PDF and persist extracted character content.")
    parser.add_argument("--pdf", required=True, help="Input PDF path")
    parser.add_argument("--out", required=True, help="Output directory for preprocessed content")
    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    out_dir = Path(args.out)

    pages = extract_pdf_with_pdfplumber(pdf_path)
    summary = persist_preprocess_output(pages, out_dir, source_pdf=str(pdf_path), extractor="pdfplumber")
    print(summary)


if __name__ == "__main__":
    main()
