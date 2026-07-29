# Repository Guidelines

## Project Structure & Module Organization

Application code lives under `src/govcheck/`. The review pipeline is organized as `parsers/` → `models/` → `checks/` → `review/engine.py` → `report/`. Keep deterministic checks in `checks/rule/` and LLM-assisted checks in `checks/llm/`. Configuration is stored in `src/govcheck/config/`; `f02_scoring.yaml` is generated and should not be edited manually.

`app.py` is the Streamlit entry point, while `src/govcheck/web/api.py` serves the FastAPI API and static assets in `web/`. Build utilities belong in `scripts/`, architecture notes in `docs/`, and feature contracts in `specs/`. Tests mirror behavior in `tests/`; golden snapshots are under `tests/golden/`. Treat `data/original/` as read-only, local-only source material.

## Build, Test, and Development Commands

- `uv sync --extra dev`: install Python 3.12 dependencies and developer tools.
- `uv run pytest`: run the complete test suite.
- `uv run pytest tests/test_f02_score.py`: run one focused test module.
- `uv run ruff check src tests scripts app.py`: lint Python code.
- `uv run uvicorn govcheck.web.api:app --port 8501`: start the FastAPI/static web UI.
- `uv run streamlit run app.py`: start the Streamlit UI.
- `uv run python scripts/build_regulation_index.py --dry-run`: inspect RAG chunks without writing an index.

## Coding Style & Naming Conventions

Use four-space indentation, Python 3.12 syntax, type hints, and a 100-character line limit enforced by Ruff. Name modules and functions `snake_case`, classes `PascalCase`, and constants `UPPER_SNAKE_CASE`. Test files follow `test_<feature>.py`; test functions should describe observable behavior. Add new review behavior as a focused check and register it in the engine instead of coupling it to UI code.

## Testing Guidelines

Use pytest and provide ground-truth positive and negative cases. Mock all LLM and embedding calls; tests must not contact real endpoints. Tests requiring untracked source files must use the `local_data` marker and skip cleanly when data is absent. Write fixtures only to `tests/` or temporary directories. Run the full suite before submitting changes.

## Commit & Pull Request Guidelines

History favors concise, imperative subjects, often Conventional Commit-style, such as `fix(report): ...` or `docs: ...`. Keep each commit scoped to one concern. Pull requests should explain the user-visible impact, list validation commands, link the relevant issue/spec, and include screenshots for changes under `web/` or `app.py`.

## Security & Configuration

Copy `.env.example` only when needed and never commit `.env`, credentials, uploads, logs, generated indexes, or original governance documents. Use only approved local/internal LLM endpoints. Logs must not contain raw documents, cell values, prompts, responses, API keys, or authorization headers.
