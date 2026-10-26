# Direct dependencies

Every package named in `backend/pyproject.toml` (the runtime list and every dependency group), by its **exact PyPI name**. A package that resembles a well-known one is not that package: `httpx2` was in the scaffold and was not reviewed by exact name until Session 5 Step 3b (LEARNING_LOG). Review a dependency by its exact name, its owner and its repository, never by resemblance.

`tests/test_dependencies_documented.py` fails when `pyproject.toml` gains or loses a direct dependency that is not listed here, or when a row's PyPI link does not match its name. To add one: tell the user why, give the licence and an alternative (CLAUDE.md), check the PyPI page and its source repository by name, then add a row.

Licences were read from the installed packages' metadata on 2026-10-26. Transitive dependencies are in `backend/uv.lock` and are not listed here.

| Package | Group | License | PyPI | Source | Purpose and why chosen |
|---|---|---|---|---|---|
| `alembic` | runtime | MIT | https://pypi.org/project/alembic/ | https://alembic.sqlalchemy.org | Database migrations. The SQLAlchemy author's own tool. |
| `argon2-cffi` | runtime | MIT | https://pypi.org/project/argon2-cffi/ | https://github.com/hynek/argon2-cffi | Password hashing with Argon2id (ADR 0004). |
| `cryptography` | runtime | Apache-2.0 OR BSD-3-Clause | https://pypi.org/project/cryptography/ | https://github.com/pyca/cryptography | Ed25519 keys for signing tokens (ADR 0004). Maintained by the Python Cryptographic Authority. |
| `fastapi` | runtime | MIT | https://pypi.org/project/fastapi/ | https://github.com/fastapi/fastapi | The web framework (ADR 0001). |
| `httpx` | runtime | BSD-3-Clause | https://pypi.org/project/httpx/ | https://github.com/encode/httpx | HTTP client for the answer-model adapters (ADR 0031). Replaced `httpx2`. Also what Starlette's test client uses. |
| `markdown-it-py` | runtime | MIT | https://pypi.org/project/markdown-it-py/ | https://github.com/executablebooks/markdown-it-py | Reads Markdown documents during extraction (ADR 0020). |
| `numpy` | runtime | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | https://pypi.org/project/numpy/ | https://numpy.org | Vector maths for the embedding model (ADR 0021). |
| `onnxruntime` | runtime | MIT | https://pypi.org/project/onnxruntime/ | https://onnxruntime.ai | Runs the pinned embedding model on the CPU without PyTorch (ADR 0021). |
| `procrastinate` | runtime | MIT | https://pypi.org/project/procrastinate/ | https://procrastinate.readthedocs.io/ | The background job queue, in PostgreSQL (ADR 0015). |
| `psycopg` | runtime | LGPL-3.0-only | https://pypi.org/project/psycopg/ | https://psycopg.org/ | PostgreSQL driver (async). LGPL: used as an unmodified library. |
| `pydantic-settings` | runtime | MIT | https://pypi.org/project/pydantic-settings/ | https://github.com/pydantic/pydantic-settings | Validated settings from the environment (ADR 0004). |
| `pyjwt` | runtime | MIT | https://pypi.org/project/pyjwt/ | https://github.com/jpadilla/pyjwt | Signs and checks access tokens (ADR 0012). |
| `pypdf` | runtime | BSD-3-Clause | https://pypi.org/project/pypdf/ | https://github.com/py-pdf/pypdf | Extracts text from PDFs in the sandboxed child process (ADR 0020). |
| `python-docx` | runtime | MIT | https://pypi.org/project/python-docx/ | https://github.com/python-openxml/python-docx | Extracts text and tables from Word files (ADR 0020). |
| `python-multipart` | runtime | Apache-2.0 | https://pypi.org/project/python-multipart/ | https://github.com/Kludex/python-multipart | Reads file uploads in FastAPI (ADR 0019). |
| `redis` | runtime | MIT | https://pypi.org/project/redis/ | https://github.com/redis/redis-py | Client for Valkey (rate limits and locks, ADR 0014). |
| `sqlalchemy` | runtime | MIT | https://pypi.org/project/sqlalchemy/ | https://www.sqlalchemy.org | ORM and query layer (ADR 0011). |
| `tokenizers` | runtime | Apache Software License | https://pypi.org/project/tokenizers/ | https://github.com/huggingface/tokenizers | Tokenises text for the embedding model (ADR 0021). |
| `uvicorn` | runtime | BSD-3-Clause | https://pypi.org/project/uvicorn/ | https://uvicorn.dev/ | The server that runs the app. |
| `mypy` | dev | MIT | https://pypi.org/project/mypy/ | https://www.mypy-lang.org/ | Strict type checking (a CI gate). |
| `pytest` | dev | MIT | https://pypi.org/project/pytest/ | https://docs.pytest.org/en/latest/ | The test runner. |
| `ruff` | dev | MIT | https://pypi.org/project/ruff/ | https://github.com/astral-sh/ruff | Linting and formatting (a CI gate). |
| `click` | operator | BSD-3-Clause | https://pypi.org/project/click/ | https://github.com/pallets/click/ | The admin command line (ADR 0010). Not installed in the web image. |

## Watch list (not dependencies)

- **`httpx2`** (https://pypi.org/project/httpx2/, https://github.com/pydantic/httpx2). Replaced in Session 5 Step 3b by the established library already present (`httpx`), when it was an unverified near-name package. Checked again on 2026-10-26: by Tom Christie, "The next generation HTTP client", repository under the `pydantic` organisation (created 2026-05-11, contributors include the httpx maintainers), 22 releases, and Starlette 1.9.0's test client prefers it and deprecates the `httpx` fallback ("install `httpx2` instead"). It is probably httpx's successor. Not a dependency of this project. Revisit when Starlette drops the `httpx` fallback, or when the HTTP client is next chosen; review it by exact name, owner and repository first (the steps above). Until then the test client runs on `httpx` with Starlette's deprecation warning, locally and in CI.
