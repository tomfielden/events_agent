# events_agent
A repo for creating an agent for capturing and recommending fun events in my local area

## Development

This project uses [uv](https://docs.astral.sh/uv/) for Python, virtualenv, and dependency management.

```sh
uv sync                    # create .venv and install all dependencies
uv run events-agent        # run the app
uv run pytest              # tests
uv run ruff check .        # lint
uv run ruff format .       # format
uv run mypy src tests      # type check
uv add <package>           # add a runtime dependency
uv add --dev <package>     # add a dev dependency
```

## Project setup history

This is how the development environment was first set up, for reference.

1. **Installed uv** with Astral's official installer (`curl -LsSf https://astral.sh/uv/install.sh | sh`). It installs to `~/.local/bin` and adds that folder to the PATH in `~/.zshrc`. Homebrew was skipped because it no longer ships prebuilt packages for Intel Macs.
2. **Initialized the project** with `uv init --package --name events-agent --python 3.13`. This created:
   - `pyproject.toml`: project metadata and an `events-agent` command that runs `events_agent:main`
   - `src/events_agent/`: the package, in a `src/` layout
   - `.python-version`: pins Python 3.13, which uv downloads and manages without pyenv
3. **Added dev tools** with `uv add --dev ruff mypy pytest`. This created the `.venv` virtual environment and the `uv.lock` lockfile, which is committed so installs are reproducible.
4. **Configured the tools** in `pyproject.toml`:
   - ruff: 100-character lines; lint rules E, F, W, I, B, UP, SIM, RUF
   - mypy: strict mode
   - pytest: runs the tests in `tests/`
5. **Added a starter test** at `tests/test_smoke.py`.
6. **Added VS Code settings** in `.vscode/`: use the `.venv` interpreter, turn on pytest, format with ruff on save, and suggest the Python, Ruff and Mypy extensions.

The existing `.gitignore` already ignored `.venv/` and `.env`.
