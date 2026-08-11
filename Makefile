UV ?= uv
PYTHON ?= python3

.PHONY: test audit render-check compile diff-check verify

test:
	$(UV) run python -m unittest discover -s tests -v

audit:
	$(UV) run --script scripts/audit_public.py

render-check:
	$(UV) run --script scripts/render_dashboard.py --check

compile:
	$(PYTHON) -m py_compile scripts/*.py

diff-check:
	git diff --check

verify: test audit render-check compile diff-check
