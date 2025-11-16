# lumivox-core

Reusable core tools for Lumivox AI applications. The first public component is
structured logging built on `structlog`.

## Installation

```bash
uv add lumivox-core
```

Install Loki support when it is needed:

```bash
uv add "lumivox-core[loki]"
```

## Usage

```python
from lumivox_core.logger import LoggingConfig, configure_logging, get_logger

configure_logging(LoggingConfig(application="my-service"))
get_logger(request_id="request-1").info("request_completed")
```

Call `shutdown_logging()` before an application exits when it needs to flush
queued log records explicitly.

## Development

Repository workflows are exposed through [just](https://just.systems/). Install
`just` and `uv`, then prepare a local checkout:

```bash
just postclone
```

Run all required checks before creating a commit:

```bash
just precommit
```

Available individual commands are `just fmt`, `just fmt_check`, `just lint`,
`just lint_fix`, `just typecheck`, `just test`, `just test_all`, `just lock`,
`just lock_check`, and `just build`. Run `just` to list them locally.
