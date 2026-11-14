.PHONY: install test test-ts demo serve

install:
	uv sync
	cd sdk/ts && pnpm install

test:
	uv run pytest -q

test-ts:
	cd sdk/ts && pnpm test

demo:
	uv run python scripts/demo.py

serve:
	uv run cellblock serve
