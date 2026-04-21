# Plan 53 — Multi-backend HA database support for pv_ml_learner

## Motivation

`pv_ml_learner` reads historic PV production from the Home Assistant recorder
database. The current implementation hardcodes the SQLite `file:...?mode=ro`
URI scheme in `ha_actuals.build_ha_engine()` and exposes only a raw filesystem
path in `HomeAssistantConfig.db_path`. This makes it impossible to connect to a
PostgreSQL or MariaDB HA instance.

`baseload_ha_db` already solved the same problem correctly: it exposes a full
SQLAlchemy URL (`homeassistant.db_url`) and delegates driver selection to the
URL scheme. The existing SQL queries in `ha_actuals.py` use only standard ANSI
SQL (no SQLite-specific syntax), so only the engine factory and the config field
need to change.

---

## Relevant source locations

```
mimirheim_helpers/pv/pv_ml_learner/pv_ml_learner/config.py
mimirheim_helpers/pv/pv_ml_learner/pv_ml_learner/ha_actuals.py
mimirheim_helpers/pv/pv_ml_learner/pv_ml_learner/__main__.py
mimirheim_helpers/pv/pv_ml_learner/tests/unit/test_config.py
mimirheim_helpers/pv/pv_ml_learner/tests/unit/test_ha_actuals.py
pyproject.toml
```

## IMPLEMENTATION_DETAILS sections

§1 (Pydantic models — extra="forbid"), §6 (boundary rules).

---

## Design decisions

### Config field: `db_path` → `db_url`

`HomeAssistantConfig.db_path: str` is replaced by `HomeAssistantConfig.db_url: str`.

The field accepts any SQLAlchemy URL. The description documents the three
expected patterns:

```
sqlite:////config/home-assistant_v2.db
postgresql+psycopg2://user:pass@host/homeassistant
mysql+pymysql://user:pass@host/homeassistant
```

A `@model_validator(mode="after")` rejects any value that does not contain
`://` with a `ValueError` that names the correct format, directing users
away from bare filesystem paths.

### Engine factory: SQLite read-only gate

`build_ha_engine(db_path: str)` → `build_ha_engine(db_url: str)`.

The SQLite `?mode=ro` connect arg and the `check_same_thread=False` connect arg
are gated on `db_url.startswith("sqlite")`. Non-SQLite URLs receive no extra
connect args.

```python
def build_ha_engine(db_url: str) -> sa.Engine:
    if db_url.startswith("sqlite"):
        # SQLite URI syntax with read-only flag.
        # check_same_thread=False is required because the ingest job
        # runs on a background APScheduler thread.
        # mode=ro prevents accidental writes even when caller code has a bug.
        connect_args = {"check_same_thread": False}
        if "?uri=true" not in db_url and "file:" not in db_url:
            # Plain sqlite:////path — convert to read-only URI form.
            path = db_url[len("sqlite:///"):]
            encoded = path.replace(" ", "%20")
            db_url = f"sqlite:///file:{encoded}?uri=true&mode=ro"
        return sa.create_engine(db_url, connect_args=connect_args)
    return sa.create_engine(db_url)
```

### `_ingest_pv_actuals_from_ha` signature

```python
def _ingest_pv_actuals_from_ha(
    array_cfg: ArrayConfig,
    ha_db_url: str,       # was: ha_db_path
    start_ts: int,
) -> list:
```

The call site in `PvLearnerDaemon._run_training_cycle` passes
`cfg.homeassistant.db_url` instead of `cfg.homeassistant.db_path`.

### Driver extras in pyproject.toml

Two new optional extras are added alongside the existing `pv-ml-learner` group:

```toml
pv-ml-learner-postgres = ["psycopg2-binary>=2.9"]
pv-ml-learner-mysql    = ["pymysql>=1.1"]
```

These mirror the existing `baseload-ha-db-postgres` and `baseload-ha-db-mysql`
extras. The base `pv-ml-learner` extra remains unchanged (SQLAlchemy is already
present; the dialect driver is the user's responsibility for non-SQLite backends).

---

## TDD workflow

### Step 1 — write failing tests

#### `tests/unit/test_config.py` — add to `TestPvLearnerConfig`

```python
def test_homeassistant_accepts_sqlite_url(self) -> None:
    """A sqlite:/// URL is accepted."""
    cfg = make_minimal_config()
    cfg["homeassistant"]["db_url"] = "sqlite:////config/home-assistant_v2.db"
    del cfg["homeassistant"]["db_path"]  # old field removed
    PvLearnerConfig.model_validate(cfg)  # must not raise

def test_homeassistant_accepts_postgres_url(self) -> None:
    """A postgresql:// URL is accepted."""
    cfg = make_minimal_config()
    cfg["homeassistant"]["db_url"] = "postgresql+psycopg2://user:pass@host/ha"
    del cfg["homeassistant"]["db_path"]
    PvLearnerConfig.model_validate(cfg)

def test_homeassistant_accepts_mysql_url(self) -> None:
    """A mysql:// URL is accepted."""
    cfg = make_minimal_config()
    cfg["homeassistant"]["db_url"] = "mysql+pymysql://user:pass@host/ha"
    del cfg["homeassistant"]["db_path"]
    PvLearnerConfig.model_validate(cfg)

def test_homeassistant_rejects_bare_path(self) -> None:
    """A bare filesystem path without a scheme raises ValidationError."""
    cfg = make_minimal_config()
    cfg["homeassistant"]["db_url"] = "/config/home-assistant_v2.db"
    del cfg["homeassistant"]["db_path"]
    with pytest.raises(PydanticValidationError):
        PvLearnerConfig.model_validate(cfg)

def test_homeassistant_rejects_old_db_path_field(self) -> None:
    """The removed db_path field is rejected by extra='forbid'."""
    cfg = make_minimal_config()
    cfg["homeassistant"]["db_path"] = "/config/home-assistant_v2.db"
    del cfg["homeassistant"]["db_url"]   # new field absent
    with pytest.raises(PydanticValidationError):
        PvLearnerConfig.model_validate(cfg)
```

Note: the existing `make_minimal_config()` fixture in `test_config.py` currently
sets `homeassistant.db_path`. Update it to `db_url` after the tests above are
written and confirmed failing.

#### `tests/unit/test_ha_actuals.py` — update existing engine test

The existing `test_build_ha_engine_returns_engine` test calls
`build_ha_engine("/config/homeassistant_v2.db")`. Change it to pass a
SQLAlchemy URL:

```python
def test_build_ha_engine_accepts_sqlite_url(self) -> None:
    """build_ha_engine accepts a plain sqlite:/// URL."""
    from pv_ml_learner.ha_actuals import build_ha_engine
    engine = build_ha_engine("sqlite:///:memory:")
    assert engine is not None

def test_build_ha_engine_accepts_sqlite_file_url(self) -> None:
    """build_ha_engine accepts a sqlite:///file: read-only URL."""
    from pv_ml_learner.ha_actuals import build_ha_engine
    engine = build_ha_engine("sqlite:///file:/tmp/test.db?uri=true&mode=ro")
    assert engine is not None
```

Run `uv run pytest` and confirm the new tests fail before implementation.

### Step 2 — implement

In this order:

1. **`config.py`**: rename `HomeAssistantConfig.db_path` → `db_url`. Add
   `@model_validator(mode="after")` that checks `"://" in self.db_url`. Update
   description, `ui_label`, and the docstring.

2. **`ha_actuals.py`**: rename `build_ha_engine(db_path)` → `build_ha_engine(db_url)`.
   Implement the SQLite gate described above. Update docstring.

3. **`__main__.py`**: rename parameter `ha_db_path` → `ha_db_url` in
   `_ingest_pv_actuals_from_ha`. Update call site at line ~456 to pass
   `cfg.homeassistant.db_url`.

4. **`pyproject.toml`**: add `pv-ml-learner-postgres` and `pv-ml-learner-mysql`
   extras.

5. **`test_config.py`**: update the `make_minimal_config()` fixture to use
   `db_url` instead of `db_path`.

### Step 3 — verify

```bash
uv run pytest mimirheim_helpers/pv/pv_ml_learner/tests/ -q --tb=short
uv run pytest -q --tb=short   # full suite — no regressions
```

All five new tests must pass. All pre-existing `test_ha_actuals.py` tests must
continue to pass (they use `sqlite:///:memory:` engines directly and are
unaffected by the config rename).

---

## Acceptance criteria

- [ ] `HomeAssistantConfig` has `db_url: str`, no `db_path`.
- [ ] `HomeAssistantConfig` validator rejects bare paths (`/config/...`) with a `ValueError`.
- [ ] `HomeAssistantConfig` accepts `sqlite://`, `postgresql+psycopg2://`, `mysql+pymysql://` URLs.
- [ ] `build_ha_engine` accepts a full SQLAlchemy URL; SQLite-specific connect args are gated on URL scheme.
- [ ] `_ingest_pv_actuals_from_ha` passes `db_url` not `db_path`.
- [ ] `pyproject.toml` has `pv-ml-learner-postgres` and `pv-ml-learner-mysql` extras.
- [ ] All five new unit tests pass.
- [ ] Full `uv run pytest` green with no regressions.
- [ ] `uv run python3 -c "from pv_ml_learner.config import PvLearnerConfig"` exits 0.
