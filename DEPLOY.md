# Deploying AskShelf

AskShelf is one Python process (Starlette) that serves the website, the app and the API. It listens on `$PORT` (default 8000) on `0.0.0.0`.

## Before you deploy
* **Set `APP_PASSWORD`.** The role switcher in the app is a demo control, not a login, so without a password anyone with the URL can act as Owner, use the Data Lab and spend your Gemini key. With `APP_PASSWORD` set the whole site asks for it (user `askshelf`, or set `APP_USER`).
* **Secrets** (`GEMINI_API_KEY`, `APP_PASSWORD`) go in the host's secret store, never in the repo. Rotate any key that has ever been committed.
* **State is in memory.** Uploaded data, report jobs and rate-limit counters live in the process, and `audit/` and `feedback/` are written to the container disk. Run **one instance** and expect them to reset on restart. A real multi-user product would move these to a database and object storage.

## Google Cloud Run
```bash
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
gcloud services enable run.googleapis.com cloudbuild.googleapis.com secretmanager.googleapis.com artifactregistry.googleapis.com

printf '%s' 'YOUR_GEMINI_KEY' | gcloud secrets create gemini-api-key --data-file=-
printf '%s' 'CHOOSE_A_PASSWORD' | gcloud secrets create askshelf-password --data-file=-

# let Cloud Run read the secrets
PN=$(gcloud projects describe YOUR_PROJECT_ID --format='value(projectNumber)')
for s in gemini-api-key askshelf-password; do
  gcloud secrets add-iam-policy-binding $s --member="serviceAccount:$PN-compute@developer.gserviceaccount.com" --role=roles/secretmanager.secretAccessor
done

gcloud run deploy askshelf --source . --region australia-southeast1 --allow-unauthenticated \
  --memory 2Gi --cpu 2 --timeout 300 --max-instances 1 --session-affinity --no-cpu-throttling \
  --set-secrets GEMINI_API_KEY=gemini-api-key:latest,APP_PASSWORD=askshelf-password:latest
```
`--max-instances 1` and `--no-cpu-throttling` matter: uploads and report jobs live in one process, and reports are built in a background thread. `--allow-unauthenticated` only lets the request reach the app; the app's own password gate then applies. The deploy prints the service URL. Cloud Build builds the `Dockerfile` for you (no local Docker needed).

## Render / Railway / Fly.io
Create a Web Service from this repo using the `Dockerfile` (or `pip install -r requirements.txt` and `python server.py`). Add `GEMINI_API_KEY` and `APP_PASSWORD` as environment variables, set the health check path to `/healthz`, and use a plan with at least 1 GB of memory.

## Check it works
Open the URL, sign in, ask a question, open Data Lab and press "Try the sample wholesale data", then generate a report. `GET /healthz` returns `ok` without a password.
