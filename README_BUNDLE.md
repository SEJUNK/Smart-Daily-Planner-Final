# To be Moved to GIT

This folder is a **copy** of the Smart Daily Planner **application code and deployment assets** (original files in the project root are unchanged).

**Included:** `api`, `agents`, `tools`, `config`, `mcp_servers`, `ui`, `requirements.txt`, `Dockerfile`, `deploy.sh`, `start.bat`, `start.sh`, Cloud Build / Firestore / Run YAML, `.gitignore`, `.env.example`.

**Excluded (by design):** `docs/`, `diagrams/`, `export/`, `__pycache__`, credentials, and optional PPT generator scripts. Regenerate the bundle with:

```powershell
powershell -ExecutionPolicy Bypass -File "scripts\copy_to_git_bundle.ps1"
```

Run from the repository root. After your review, initialize git here or copy this folder into your new repository.

See `VIDEO_RECORDING_FLOW.md` and `DEMO_VOICEOVER_TTS.md` for the demo.
