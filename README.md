# Finance App Database Service

A Python 3.11 FastAPI database service for the Finance Product.

## Ticker Symbol Normalization

All ticker creation and history lookup endpoints now normalize and validate
ticker symbols **before any database access**.

### Normalization Rules

1. **Trim** leading and trailing whitespace.
2. **Uppercase** the symbol.
3. **Validate** against the structural regex: `^[A-Z][A-Z0-9.\-]{0,9}$`

Valid tickers are 1–10 characters, start with a letter, and contain only
letters, digits, dots (`.`), or hyphens (`-`).

### HTTP 422 Response

If a ticker symbol fails validation, the API returns a stable **HTTP 422**
response with the following JSON body:

```json
{"detail": "Invalid ticker symbol"}
```

This happens for:
- Empty or whitespace-only strings
- Symbols starting with a digit
- Symbols containing invalid characters (e.g. `@`, `_`, `!`)
- Symbols longer than 10 characters

### Affected Endpoints

- `POST /tickers` — validates `ticker` field before insert
- `GET /history` — validates `ticker_name` query parameter before lookup
- `GET /history/last_date` — validates `ticker_name` query parameter before lookup

## Build

```bash
docker build --platform linux/amd64 . -t registry.lucas.engineering/finance_app_database_service:0.5
docker push registry.lucas.engineering/finance_app_database_service:0.5
```
