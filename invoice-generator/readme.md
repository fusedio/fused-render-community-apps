# Invoice Generator

![Client book with the New client dialog open](preview.png)

A local invoice manager. Create clients, issue numbered invoices, track
status, and print. Nothing leaves your machine: every document is a plain
JSON file on disk, one invoice per file, grouped per client — readable,
diffable, yours.

- Client book with per-client invoice numbering.
- Invoice editor with line items, taxes, and FX reference rates.
- Attachments: a bill, receipt or any supporting PDF or image rides along
  as extra pages of the same document.
- Print-ready invoice layout straight from the browser.
- Pure-stdlib Python backend (`invoice.py`) — no dependencies to install.
- Every backend action returns plain dicts, so an AI agent can drive the
  whole app headlessly exactly as the UI does.

## Attachments

Open **Attachments** in the invoice editor and add a PDF or an image. A PDF is
split into one sheet per page, an image becomes one sheet, and every sheet is
laid out A4 and printed *after* the invoice — so the file you get from
**Download PDF** is a single document: invoice first, the bill copy behind it.

Give each one a label ("Hotel bill", "AWS receipt") and the invoice lists them —
*Attachment 1 — Hotel bill, 2 pages* — so a line item has something to point at.
Leave the label empty and it prints as plain *Attachment 1*. Rows reorder and
delete from the same panel.

Pages are stored as image files beside the invoice, under
`clients/<client>/attachments/<invoice id>/`, and the invoice JSON only points
at them: the document file stays small and autosave stays quick no matter how
many scans you attach. Deleting an invoice deletes its pages; duplicating one
copies them.

One caveat on the "nothing leaves your machine" promise: turning a PDF into page
images needs [pdf.js](https://mozilla.github.io/pdf.js/), which is fetched from
`cdn.jsdelivr.net` the first time you attach a PDF after opening the app. Your
PDF is read and rendered locally and is never uploaded anywhere, and image
attachments need no network at all — but that one script download does happen.
