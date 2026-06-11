# Image Integration Testing Playbook (saved per integration playbook directive)

## Rules
- Base64-encoded images only (JPEG/PNG/WEBP).
- No blank/solid images — real visual features required.
- For docket scanner tests, simulate a real photo (text, lines, table structure) rather than a flat-colour rectangle.
- Re-detect MIME after any transcoding step.
- Animated images → extract first frame only.
- Resize large payloads.

## Endpoint under test
`POST /api/scan-docket/ingham`
Body: `{ "imageData": "<base64 jpeg/png>", "mimeType": "image/jpeg" | "image/png" }`
Expected: `{ "ok": true, "fields": { "feedType": ..., "amount": ..., "deliveryDate": "YYYY-MM-DD", ... } }`
