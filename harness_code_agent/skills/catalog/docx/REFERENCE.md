# DOCX reference

## Content coverage

A complete inspection may need more than `document.paragraphs`:

- paragraphs and runs;
- tables, including nested tables;
- headers and footers for every section;
- inline shapes and image relationships;
- styles, numbering, sections, margins, orientation, and page size;
- hyperlinks, fields, comments, footnotes/endnotes, text boxes, and tracked revisions in package XML.

For a simple summary, paragraphs and table cells are usually sufficient. For editing or fidelity claims, inspect the structures that could be affected.

## Editing rules

- Prefer updating runs in place when formatting must survive. Replacing `paragraph.text` recreates runs and can discard formatting.
- Use existing named styles before creating new ones.
- Keep section breaks and header/footer linkage intact.
- When inserting images, use explicit dimensions and preserve aspect ratio.
- Do not accept invalid XML relationships or dangling media references. Reopen the saved package as a validation step.
- Low-level XML edits must use the WordprocessingML namespace and target the smallest possible element set.

## Conversion and visual QA

Use a local LibreOffice executable when available. On Windows, a common headless command is:

`"C:\Program Files\LibreOffice\program\soffice.com" --headless --convert-to pdf --outdir <output-dir> <input.docx>`

Confirm the PDF was created before inspecting it. Load `catalog/pdf/SKILL.md` for PDF rendering and validation. Compare every affected page for clipping, unexpected reflow, missing glyphs, misplaced images, and broken headers or footers.

## Completion checks

At minimum: the output exists, opens as a DOCX ZIP package, can be loaded by `python-docx`, and contains the intended structural change. For layout-sensitive tasks, also convert and visually inspect the affected pages.
