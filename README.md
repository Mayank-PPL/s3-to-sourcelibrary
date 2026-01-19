# S3 to Source Library Migration Tool

A robust Python tool for migrating 546GB of book page scans from AWS S3 to a web application, with metadata from CSV files, comprehensive failure handling, progress tracking, and resume capabilities.

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
- 546GB of book scans in S3
- Books CSV (~3,075 rows) with metadata
- Pages CSV (490K rows) with file locations

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

4. Configure environment:
```bash
cp .env.example .env
# Edit .env with your actual credentials and settings
```

## Configuration

Create a `.env` file in the project root with the following settings:

```env
# S3 Configuration
AWS_ACCESS_KEY_ID=your_access_key
AWS_SECRET_ACCESS_KEY=your_secret_key
AWS_REGION=us-east-1
S3_BUCKET_NAME=bucket-name
S3_BASE_PREFIX=path/to/files

# API Configuration
API_BASE_URL=https://your-api.com
BOOK_CREATE_ENDPOINT=/api/books
PAGE_UPLOAD_ENDPOINT=/api/upload

# CSV Configuration
BOOKS_CSV_PATH=./data/csv/books.csv
PAGES_CSV_PATH=./data/csv/pages.csv.zip

# Migration Settings
BOOK_WORKERS=3
MAX_RETRIES=3
RETRY_BACKOFF=2.0

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
- `Filename` (without extension)
- `DAM Directory` (S3 path relative to base prefix)

## Usage

### Autonomous Mode (Recommended for EC2)
Automatically indexes if needed, then migrates:
```bash
python -m src.main
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

## Architecture

### Directory Structure
```
s3-to-sourcelibrary-tool/
├── data/
│   ├── csv/              # CSV files
│   └── index/            # SQLite database
├── logs/                 # Log files
├── temp/                 # Temporary S3 downloads
├── src/                  # Source code
│   ├── main.py          # CLI entry point
│   ├── config.py        # Configuration
│   ├── csv_handler.py   # CSV parsing
│   ├── s3_locator.py    # S3 operations
│   ├── state_manager.py # Database management
│   ├── api_client.py    # API communication
│   ├── orchestrator.py  # Migration coordinator
│   └── utils.py         # Utilities
├── requirements.txt
└── README.md
```

### Migration Flow

1. **Indexing Phase** (one-time or on-demand):
   - Parse Books CSV
   - Parse Pages CSV
   - Map filenames to S3 keys
   - Verify file existence
   - Store in SQLite database

2. **Migration Phase** (resumable):
   - Process books concurrently (configurable workers)
   - For each book:
     - Create book via API
     - Upload pages **sequentially** in correct order
     - Download from S3 → Upload to API → Delete temp file
   - Track progress in database
   - Handle failures with retry logic

### Key Design Decisions

- **Sequential Page Uploads**: Pages must upload one-by-one per book to maintain order
- **Concurrent Books**: Multiple books can process in parallel
- **Temp File Management**: One page at a time per worker, immediate cleanup
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
SELECT * FROM missing_files LIMIT 10;
```

### Progress Tracking
The tool shows:
- Books completed/pending/failed
- Pages uploaded/pending/failed
- Data uploaded (GB)
- ETA and upload speed

## Troubleshooting

### Missing Files
Check the `missing_files` table:
```sql
SELECT * FROM missing_files;
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
- Rate limiting: Reduce BOOK_WORKERS in .env
- Authentication: Verify API_BASE_URL in .env

## Production Deployment (EC2)

1. Launch EC2 instance with sufficient disk space for temp files
2. Install dependencies
3. Place CSV files in `data/csv/`
4. Configure `.env` with production credentials
5. Run in background:
```bash
nohup python -m src.main > output.log 2>&1 &
```

6. Monitor progress:
```bash
python -m src.main status
tail -f logs/migration.log
```

## License

MIT License

## Support

For issues or questions, please open an issue on GitHub.
