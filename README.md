# SPPU Questions Server

**SPPU Questions Server** is an automated backend system designed to extract, process, and semantically group university exam questions from raw PDF papers.
This backend handles everything from OCR conversion to intelligent clustering, HTML snapshot generation, and cloud synchronization.

## 📌 Features

- **Automated PDF Processing** – Leverages Mistral OCR to reliably convert raw PDF exam papers into structured Markdown.
- **Semantic Question Grouping** – Uses text embeddings and Agglomerative Clustering to intelligently group similar or repeated questions across different papers.
- **HTML & Snapshot Generation** – Converts markdown content to HTML and uses Playwright to capture high-quality snapshots of questions.
- **Cloud Integration** – Seamlessly integrates with Supabase for robust database management and scalable cloud storage of generated artifacts and images.
- **FastAPI Backend** – High-performance Python web framework offering speed, automatic documentation, and asynchronous background task orchestration.
- **Secure Key Management** – Includes endpoints to securely encrypt and store user API keys.

## 🧰 Technologies Used

- **Backend Server:** FastAPI, Uvicorn
- **Cloud Database & Storage:** Supabase
- **Document Processing:** Mistral OCR, PDF Parsing
- **Machine Learning & NLP:** Embeddings, Agglomerative Clustering
- **Web Automation:** Playwright (for taking visual snapshots)
- **Programming Language:** Python 3

## ⚙️ Installation

1. **Clone the repository**
   ```bash
   git clone https://github.com/YNJ27/SmartPrep_backend.git
   ```

2. **Create a virtual environment**
   ```bash
   python -m venv .venv
   ```
   Activate it:
   - Windows:
     ```bash
     .venv\Scripts\activate
     ```
   - macOS/Linux:
     ```bash
     source .venv/bin/activate
     ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```
   *(Note: You may also need to run `playwright install` to install the required browsers for snapshot generation).*

4. **Environment Variables**
   Create a `.env` file in the root directory and add the necessary credentials:
   ```env
   SUPABASE_URL=your_supabase_url
   SUPABASE_SERVICE_ROLE_KEY=your_supabase_key
   # Add other required API keys (e.g., for Mistral)
   ```

5. **Start the FastAPI server**
   ```bash
   python main.py
   ```
   *(Or run via `uvicorn main:app --reload`)*

## 📡 API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| **POST** | `http://localhost:8000/subjects/import-pdfs` | Imports PDFs, downloads them, and starts the processing orchestrator in the background. |
| **GET** | `http://localhost:8000/subjects/status/{file_hash}` | Checks the current processing status of uploaded PDFs based on their file hash. |
| **GET** | `http://localhost:8000/subjects/{subject}/grouped-questions` | Retrieves all grouped unit question JSONs for a specific subject from cloud storage. |
| **POST** | `http://localhost:8000/user/api-keys` | Securely encrypts and saves user API keys (e.g., Google/Mistral keys) for processing. |
