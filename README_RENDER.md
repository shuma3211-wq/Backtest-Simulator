# FTO Backtest Simulator v12

This version is designed so GitHub does NOT contain the large historical CSV.
The server downloads a gzip-compressed USDJPY M5 dataset from `DATA_URL` on first startup.

## 1. GitHub
Upload all files in this folder to the repository root. Keep the `data/` folder empty.

## 2. Historical data
The supplied 64 MB CSV is also provided separately as `USDJPY_M5.csv.gz`.
Upload that `.gz` file to a GitHub Release (not the repository file list). A Release asset can be much larger than the browser repository upload limit.
Copy the asset's direct download URL.

## 3. Render
Create a Web Service from the GitHub repository.
- Plan: Free
- Build: `pip install -r requirements.txt`
- Start: `uvicorn server:app --host 0.0.0.0 --port $PORT`
- Health Check: `/api/info`

Add an environment variable:
- Key: `DATA_URL`
- Value: direct URL to `USDJPY_M5.csv.gz` from the GitHub Release

The first start downloads and extracts the data automatically. Future starts can download it again because Render Free files are ephemeral.
