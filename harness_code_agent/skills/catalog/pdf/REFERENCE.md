# PDF reference

## Capability map

| Need | Preferred tool | Validation |
|---|---|---|
| Metadata, page count, text | `pypdf.PdfReader` | Reopen; compare page count and sampled text |
| Tables, words with coordinates | `pdfplumber` | Inspect representative pages and table boundaries |
| Render pages | `pypdfium2` | Render at 150-200 DPI and inspect images |
| Merge, split, rotate, crop | `pypdf.PdfWriter` | Reopen output; verify order, boxes, rotation |
| Generate a PDF | ReportLab | Reopen and render every page |
| OCR scanned pages | Render, then a locally available OCR engine | Compare OCR against visible page regions |

## Inspection pattern

Use a small Python probe that prints only bounded metadata and a short text sample. Check `reader.is_encrypted` before extraction. If encrypted, attempt only credentials explicitly supplied by the user.

For each representative page, distinguish:

- text extraction returned meaningful text;
- extraction returned empty or near-empty text and the rendered page is image-based;
- extraction produced scrambled text because of font encoding or document damage.

Only the second case clearly calls for OCR. The third may need alternate extraction or visual transcription.

## Transformations

- Merge in the user-specified order; never rely on filesystem enumeration order.
- Split using one-based page language in user-facing messages and convert carefully to zero-based library indices.
- Rotation changes presentation; cropping changes page boxes. Validate both rendered appearance and page boxes.
- Preserve metadata, outlines, forms, annotations, and attachments when the task requires them; `pypdf` operations do not preserve every structure automatically.
- For generated documents, embed suitable fonts when non-ASCII text matters. A successful write does not prove glyphs rendered correctly.

## Completion checks

At minimum: output exists, opens without error, has the expected page count, and satisfies a content check. For visual or layout-sensitive work, render the result and inspect every affected page.
