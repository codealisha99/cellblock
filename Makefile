.PHONY: install test test-ts demo record serve

install:
	uv sync
	cd sdk/ts && pnpm install

test:
	uv run pytest -q

test-ts:
	cd sdk/ts && pnpm test

demo:
	uv run python scripts/demo.py

record:
	uv run python scripts/record_session.py  # regenerates site/data from a real Docker run

serve:
	uv run cellblock serve
