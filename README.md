# S3 to Source Library Migration Tool

A robust Python tool for migrating book page scans from AWS S3 to a web application, with metadata from CSV files, comprehensive failure handling, progress tracking, and resume capabilities.

## Features

- **Two-Phase Architecture**: Index once, migrate repeatedly
- **Resume Capability**: Restart anytime without duplicates
- **Per-Page Tracking**: Granular progress tracking for each page
- **Sequential Page Uploads**: Maintains page order (critical for books)
- **Concurrent Book Processing**: Parallel processing of multiple books
- **Automatic File Discovery**: Tries multiple extensions for files without extensions in CSV
- **Comprehensive Logging**: Detailed logs for debugging and monitoring
- **Data Integrity**: Tracks all operations in SQLite database

## Requirements

- Python 3.11+
- AWS S3 access
- Book scans in S3
- Books CSV (~3,075 rows) with metadata
- Pages CSV (490K rows) with file locations
- Target API reachable from the machine running this tool
   - The API must be able to download from S3 using presigned URLs (the API pulls images directly from S3)

## Installation

1. Clone the repository:
```bash
git clone <repository-url>
cd s3-to-sourcelibrary-tool
```

2. Create virtual environment:
```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

4. Create a `.env` file in the project root (see the next section for required keys).

5. Provide the input CSVs in `data/csv/`.

By default, the tool looks for:
- `data/csv/ScannedBooks.csv`
- `data/csv/PageScans.csv.zip`

(You can override these paths via `BOOKS_CSV_PATH` and `PAGES_CSV_PATH` in `.env`.)

6. (Optional) Resume from an existing migration state (handoff to another user/machine).

If you want another person/machine to continue from your exact migration status (instead of restarting), export a bundle from the machine with the latest state:
```bash
scripts/handoff_export.sh handoff/state_and_inputs.tgz
```

Send `handoff/state_and_inputs.tgz` (and the generated `.sha256` file, if present) to the other user.

On the new machine (inside the cloned repo), import it:
```bash
scripts/handoff_import.sh /path/to/state_and_inputs.tgz
```

What the bundle contains:
- `data/csv/` (input CSVs)
- `data/index/migration_state.db` (a consistent SQLite snapshot made via `sqlite3 .backup`)

What the bundle does not contain:
- `.env` (credentials/config)
- `logs/`, `temp/`, or source code

Note: avoid running two migrations concurrently from the same handed-off state; this workflow is intended for one runner at a time.

## Configuration

Create a `.env` file in the project root with the following settings.

Notes:
- Only `API_BASE_URL` is configurable; the tool currently uses fixed paths under that base URL.
- `TEMP_DIR` is kept for legacy/local-download workflows; the current migration flow does not require downloading page images to disk.

```env
# S3 Configuration
AWS_ACCESS_KEY_ID=your_access_key
AWS_SECRET_ACCESS_KEY=your_secret_key
AWS_REGION=eu-central-1
S3_BUCKET_NAME=bucket-name
S3_BASE_PREFIX=collection/export_dam_files/jp2

# API Configuration
API_BASE_URL=https://your-api.com
SL_API_SECRET=your_api_secret

# CSV Configuration
BOOKS_CSV_PATH=./data/csv/ScannedBooks.csv
PAGES_CSV_PATH=./data/csv/PageScans.csv.zip

# Migration Settings
BOOK_WORKERS=1
MAX_RETRIES=3
RETRY_BACKOFF=2.0
REQUEST_DELAY=1.0

# Paths
TEMP_DIR=./temp
STATE_DB_PATH=./data/index/migration_state.db
LOG_FILE=./logs/migration.log

# Logging
LOG_LEVEL=INFO
```

## CSV Format

### Books CSV
Must contain these columns:
- `Picturae barcode` (primary key)
- `Title`
- `Author`
- `Editor`
- `Place of publication`
- `Printer`
- `Publisher`
- `Year of publication`
- `Language`

### Pages CSV
Must contain these columns:
- `Code` (matches `Picturae barcode` from Books CSV)
- `Filename` (typically without extension; the tool will try common image extensions)
- `DAM Directory` (S3 path relative to base prefix)

## Usage

### Autonomous Mode for EC2
Automatically indexes if needed, then migrates:
```bash
python -m src.main
```

Show all commands/options:
```bash
python -m src.main --help
```

### Manual Commands

#### 1. Index CSV Files
Build the index database from CSV files:
```bash
python -m src.main index
```

Force re-index:
```bash
python -m src.main index --force
```

#### 2. Verify Index
Check index statistics:
```bash
python -m src.main verify-index
```

#### 3. Run Migration
Migrate all books:
```bash
python -m src.main migrate
```

Test with limited books:
```bash
python -m src.main migrate --limit 10
```

#### 4. Check Status
View migration progress:
```bash
python -m src.main status
```

#### 5. Verify Data Integrity
After migration completes, verify all data was migrated correctly:
```bash
python -m src.main verify
```

Test with limited books:
```bash
python -m src.main verify --limit 10
```

Export verification report to CSV:
```bash
python -m src.main verify --export verification_report.csv
```

The verify command checks:
- All completed books exist in the API
- Page counts match between database and API
- All page IDs are present in the API
- Reports any mismatches or missing data

#### 6. Reset Operations
Reset specific book:
```bash
python -m src.main reset --book RIT001001887
```

Reset all migration (keeps index):
```bash
python -m src.main reset --all-migration
```

Full reset (deletes everything):
```bash
python -m src.main reset --full
```

#### 7. Update Books

Manually trigger the `update_book_after_upload` API call for eligible books. This is useful for retroactively notifying the API about books that were successfully uploaded but may have missed the final update notification.

**Eligibility Criteria:**
- Books with status `completed`: All books with this status are eligible
- Books with status `in_progress`: Only if they have zero pending pages (meaning all pages uploaded)

**Usage:**

Preview what would be updated (dry-run):
```bash
python -m src.main update-books --dry-run
```

Update all eligible books:
```bash
python -m src.main update-books
```

Update only completed books:
```bash
python -m src.main update-books --completed-only
```

Update only in-progress books with no pending pages:
```bash
python -m src.main update-books --in-progress-only
```

Update specific books by barcode:
```bash
python -m src.main update-books --books RIT001001177,RIT001001179
```

Update with custom retry count:
```bash
python -m src.main update-books --max-retries 5
```

Limit to first N eligible books:
```bash
python -m src.main update-books --limit 10
```

**Options:**
- `--dry-run`: Preview which books would be updated without making API calls
- `--books`: Comma-separated list of specific barcodes to update
- `--completed-only`: Only update books with status "completed"
- `--in-progress-only`: Only update books with status "in_progress" that have no pending pages
- `--max-retries`: Maximum retry attempts per book (default: 3)
- `--limit`: Limit to first N eligible books (for testing)

## Architecture

### Directory Structure
```
s3-to-sourcelibrary-tool/
├── data/
│   ├── csv/              # CSV files
│   └── index/            # SQLite database
├── logs/                 # Log files
├── temp/                 # Legacy temp downloads (not required for current presigned-URL flow)
├── src/                  # Source code
│   ├── main.py          # CLI entry point
│   ├── config.py        # Configuration
│   ├── csv_handler.py   # CSV parsing
│   ├── s3_locator.py    # S3 operations
│   ├── state_manager.py # Database management
│   ├── api_client.py    # API communication
│   ├── orchestrator.py  # Migration coordinator
│   ├── book_updater.py  # Re-send "update-book" for eligible books
│   ├── verifier.py      # Post-migration verification
│   └── utils.py         # Utilities
├── requirements.txt
└── README.md
```

### Migration Flow

1. **Indexing Phase** (one-time or on-demand):
   - Parse Books CSV
   - Parse Pages CSV
   - Map filenames to S3 keys
   - Store in SQLite database (S3 existence/size checks happen during migration)

2. **Migration Phase** (resumable):
   - Process books concurrently (configurable workers)
   - For each book:
     - Create book via API
     - Upload pages **sequentially** in correct order
       - Validate object exists in S3 → Generate presigned URL → Send URL to API
   - Track progress in database
   - Handle failures with retry logic

### Key Design Decisions

- **Sequential Page Uploads**: Pages must upload one-by-one per book to maintain order
- **Concurrent Books**: Multiple books can process in parallel
- **No Temp Downloads (Current Flow)**: The API downloads images directly from S3 via presigned URLs
- **Resume Logic**: Database tracks every operation, safe to restart anytime
- **No Duplicates**: State tracking prevents re-uploading completed pages

## Monitoring

### Logs
Check logs for detailed information:
```bash
tail -f logs/migration.log
```

### Database Queries
Inspect the SQLite database directly:
```bash
sqlite3 data/index/migration_state.db

# Example queries:
SELECT COUNT(*) FROM books WHERE migration_status = 'completed';
SELECT COUNT(*) FROM pages WHERE upload_status = 'uploaded';
SELECT picturae_barcode, filename, error_message FROM pages WHERE upload_status = 'failed' LIMIT 10;
```

### Progress Tracking
The tool shows:
- Books completed/pending/failed
- Pages uploaded/pending/failed
- Data uploaded (GB)
- (Optional) missing/failed items via database queries and logs

## Troubleshooting

### Missing Files
In the current implementation, missing objects are surfaced as failed pages (the `missing_files` table exists but is not populated).

Useful queries:
```sql
-- Pages that failed (often includes "S3 file not found" in error_message)
SELECT picturae_barcode, filename, dam_directory, error_message
FROM pages
WHERE upload_status = 'failed'
LIMIT 20;
```

### Failed Books
Query failed books:
```sql
SELECT picturae_barcode, error_message FROM books WHERE migration_status = 'failed';
```

Reset and retry:
```bash
python -m src.main reset --book <barcode>
python -m src.main migrate
```

### API Errors
Check logs for API error responses. Common issues:
- Network timeouts: Increase timeout in api_client.py
- Rate limiting: Reduce BOOK_WORKERS and/or increase REQUEST_DELAY in .env
- Authentication: Verify API_BASE_URL in .env

## License

MIT License

## Support

For issues or questions, please open an issue on GitHub.
