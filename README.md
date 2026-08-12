# SmartPrep Backend

**SmartPrep** is a Python backend for processing university exam PDFs, extracting questions, grouping similar questions, storing results in Supabase, and exposing authenticated APIs for subject tracking and study progress.

This project currently includes:
- PDF import and orchestration for subject processing
- Supabase-backed auth/session handling
- User subject saving and progress tracking
- Grouped question retrieval from storage
- Encrypted API key persistence for third-party providers
- Cloud upload support for generated snapshots and images via Cloudinary

## 📌 Features

- **Automated PDF Processing** – Downloads uploaded PDFs, runs the processing pipeline, and tracks completion status by file hash.
- **Supabase Integration** – Uses Supabase for auth, database records, and file storage of grouped question output.
- **Question Grouping** – Stores processed unit results as JSON files and exposes them via subject-based retrieval endpoints.
- **User Progress Tracking** – Saves subject metadata, group status, and question completion state per user.
- **Encrypted API Keys** – Stores user-provided provider API keys securely using a master encryption key.
- **Authentication Flow** – Accepts frontend Supabase session tokens and issues secure HTTP-only cookies for backend access.
- **Cloudinary Uploads** – Uploads generated images/snapshots to Cloudinary for storage and sharing.
- **FastAPI Backend** – Provides REST APIs for subject import, status checks, auth, and user management.

## 🧰 Technologies Used

- **Backend Server:** FastAPI, Uvicorn
- **Database & Storage:** Supabase
- **Cloud Storage:** Cloudinary
- **OCR / Document Processing:** Mistral OCR, PDF processing pipeline
- **Machine Learning / NLP:** Embeddings and clustering for semantic grouping
- **Web Automation:** Playwright
- **Languages / Tools:** Python 3, dotenv, Pydantic

## ⚙️ Installation

1. **Clone the repository**
   ```bash
   git clone https://github.com/YNJ27/SmartPrep_backend.git
   ```

2. **Create and activate a virtual environment**
   ```bash
   python -m venv .venv
   ```

   Windows:
   ```bash
   .venv\Scripts\activate
   ```

   macOS/Linux:
   ```bash
   source .venv/bin/activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

   If browser snapshot generation is used, install Playwright browsers as needed:
   ```bash
   playwright install
   ```

4. **Create a .env file**
   Add the required environment variables in the project root:
   ```env
   CLOUDINARY_API_KEY=            # Cloudinary upload API key
   CLOUDINARY_API_SECRET=         # Cloudinary upload secret
   CLOUDINARY_CLOUD_NAME=         # Cloudinary cloud name
   SUPABASE_SERVICE_ROLE_KEY=     # Supabase admin/service key
   SUPABASE_URL=                  # Supabase project URL
   MASTER_ENCRYPTION_KEY=         # Encrypts/decrypts user API keys
   COOKIE_SECURE=true             # true in production (HTTPS)
   COOKIE_DOMAIN=                 # Auth cookie domain
   FRONTEND_ORIGIN=               # Frontend URL allowed by CORS
   ```

5. **Run the project**
   ```bash
   python main.py
   ```

   Or using uvicorn directly:
   ```bash
   uvicorn main:app --reload
   ```

## 📡 Current API Endpoints

Base URL for local development: `http://localhost:8000`

| Method | Endpoint | Description |
|--------|----------|-------------|
| **POST** | `/auth/session` | Verifies frontend Supabase tokens and stores secure auth cookies in the browser. |
| **GET** | `/auth/me` | Fetches the authenticated user from the active session. |
| **POST** | `/auth/logout` | Logs out the current user and clears auth cookies. |
| **POST** | `/subjects/import-pdfs` | Downloads uploaded PDFs, validates them, and starts the background processing pipeline. |
| **GET** | `/subjects/status/{file_hash}` | Returns the current status of a processed subject using its file hash. |
| **GET** | `/subjects/{subject}/grouped-questions` | Retrieves grouped question JSON files for a subject from Supabase storage. |
| **POST** | `/user/subjects` | Saves a user-specific subject record with metadata such as branch, year, pattern, and exam type. |
| **GET** | `/user/subjects` | Lists all saved subjects for the authenticated user. |
| **DELETE** | `/user/subjects/{subject_id}` | Deletes a saved subject and clears its related progress records. |
| **GET** | `/user/progress` | Fetches the status of group progress and question completion for a subject/unit. |
| **POST** | `/user/group-status` | Saves or resets the status for a study group. |
| **POST** | `/user/question-progress` | Saves the done/undone state for a specific question. |
| **POST** | `/user/group-reset` | Resets progress for a specific group within a unit. |
| **GET** | `/user/api-keys` | Lists the API providers already stored for the current user. |
| **POST** | `/user/api-keys` | Encrypts and stores a provider API key for the authenticated user. |

## 🗂️ Notes

- The backend uses `file_hash` to avoid reprocessing the same set of PDFs.
- Subject processing is tracked in Supabase using the `processed_subjects` table.
- User-specific study state is stored in tables such as `user_subjects`, `user_group_status`, and `user_question_progress`.
- API keys saved through the `/user/api-keys` route are encrypted before storage.
