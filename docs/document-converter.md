# Document Converter Service

Small FastAPI service for converting local documents.
It is designed to run as a separate container from the existing video parsing API.

## Endpoints

- `GET /health`
- `POST /document/word-to-pdf`
- `POST /document/convert-to-pdf`
- `POST /document/pdf-to-word`

All conversion endpoints accept multipart form-data:

```bash
curl -X POST http://127.0.0.1:8010/document/word-to-pdf \
  -F "file=@sample.docx" \
  --output sample.pdf
```

```bash
curl -X POST http://127.0.0.1:8010/document/pdf-to-word \
  -F "file=@sample.pdf" \
  --output sample.docx
```

If `DOCUMENT_CONVERTER_TOKEN` is set, add:

```bash
-H "Authorization: Bearer your-token"
```

## Supported Files

Word to PDF:
- `.docx`
- `.doc`
- `.rtf`
- `.odt`

PDF to Word:
- `.pdf`

Word to PDF uses LibreOffice headless mode, so it preserves layout much better than text-only conversion.
PDF to Word uses `pdf2docx`, which works best for text-based PDFs. Scanned PDFs need a separate OCR flow.

## Docker

Build:

```bash
docker build -f Dockerfile.document-converter -t document-converter:latest .
```

Run:

```bash
docker run -d \
  --name document-converter \
  -p 8010:8010 \
  -e DOCUMENT_CONVERTER_TOKEN=change-me \
  document-converter:latest
```

Nginx reverse proxy example:

```nginx
location /document-converter/ {
    proxy_pass http://document-converter:8010/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    client_max_body_size 20m;
    proxy_read_timeout 120s;
}
```

Mini Program backend can upload the selected Word file to this API and save the returned PDF stream.
