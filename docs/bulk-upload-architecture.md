# Bulk Excel Upload – Backend Flow

## Overview
This module handles bulk upload of emission data from Excel/CSV files.
Flow:
1. Upload file
2. Map columns
3. Fetch unique categories
4. Preview first 100 rows (with emission factor + total emission)
5. Import all rows into emission table

---

## API Endpoints

### POST /v1/excel/upload
Returns:
{
  "document_id": number,
  "headers": string[]
}

### POST /v1/excel/unique-categories
Returns:
{
  "unique_categories": string[],
  "total_rows": number
}

### POST /v1/excel/preview
Returns:
{
  "rows": [...mapped rows with emission_factor & total_emission...],
  "total_rows": number
}

### POST /v1/excel/import
Returns:
{
  "inserted": number,
  "total_rows": number
}

---

## Core Files

### app/api/excel.py
- Handles API routing
- Calls service layer
- Returns JSON responses

### app/services/excel_parser.py
- Reads Excel
- Applies mappings
- Calculates emission factor & total emission
- Handles bulk import

### app/core/database.py
- DB connection
- Emission factor lookup
- Bulk insert into emission table
- uploaded_documents management

---

## Key DB Tables
- uploaded_documents
- emission
- emission_factors