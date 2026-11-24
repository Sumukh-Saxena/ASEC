# API OpenAPI document

`openapi.json` is generated, not hand-edited:

```powershell
python -m backend openapi          # write it
python -m backend openapi --check  # fail on drift, never write
```

Built from the real app with dummy settings: no database, network, secret or bound port. Default FastAPI 422 models are kept as generated (cosmetic carry-forward).
