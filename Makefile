.PHONY: help all test lint ruff interrogate fmt check-fmt markdownlint shellcheck yamllint actionlint matrix smoke-leg

UV ?= uv
UV_ENV = UV_CACHE_DIR=.uv-cache UV_TOOL_DIR=.uv-tools
RUFF_VERSION ?= 0.15.12
PYTEST_DEPS = --with 'pytest>=8,<10' --with 'pyyaml>=6,<7' --with 'hypothesis>=6,<7'
PY_SOURCES := scripts tests
SHELL_SOURCES := scripts/build_extension.sh scripts/build_in_container.sh scripts/smoke_test.sh
MDLINT ?= markdownlint-cli2
YAMLLINT ?= yamllint
ACTIONLINT ?= actionlint
# `make fmt` and `make check-fmt` call mdtablefix directly. `--git` selects the
# Markdown files Git tracks and `--include-untracked` adds the untracked files
# Git does not ignore, so a new document is formatted before it is staged.
# Both modes need mdtablefix 0.6.0 or later; CI pins the version at the
# install-mdtablefix step.
MDTABLEFIX ?= mdtablefix
MDTABLEFIX_SELECT = --git --include-untracked
MDTABLEFIX_RULES = --wrap --renumber --breaks --ellipsis --fences

all: check-fmt lint test ## Run every commit gate

test: ## Run the unit tests, docstring examples and workflow contracts
	$(UV_ENV) $(UV) run --no-project --python 3.13 $(PYTEST_DEPS) python -m pytest -q
	$(UV_ENV) $(UV) run --no-project --python 3.13 $(PYTEST_DEPS) python -m pytest -q --doctest-modules scripts

lint: shellcheck markdownlint ruff interrogate yamllint actionlint ## Lint Python, shell, Markdown, YAML and workflow sources

# Interrogate scans each target recursively, subject to its default exclusions.
INTERROGATE_VERSION ?= 1.7.0
INTERROGATE_TARGETS ?= $(PY_SOURCES)
# Level 2 prints a MISSED row per undocumented definition, which the
# `interrogate` recipe below looks for: the rounded percentage alone can pass a
# lone gap.
INTERROGATE_FLAGS ?= -vv --fail-under 100
INTERROGATE ?= $(UV_ENV) $(UV) tool run --python 3.13 --from 'interrogate==$(INTERROGATE_VERSION)' interrogate

# Interrogate rounds its percentage to one decimal, so `--fail-under 100` alone
# can pass a lone gap in a large repository. This recipe also fails when `grep`
# finds a MISSED row in the report on standard output, which `-vv` or higher
# prints for each undocumented definition.
interrogate: ## Enforce complete Python docstring coverage
	@report="$$($(INTERROGATE) $(INTERROGATE_FLAGS) $(INTERROGATE_TARGETS))"; \
	  status=$$?; \
	  printf '%s\n' "$$report"; \
	  test "$$status" -eq 0 || exit "$$status"; \
	  if printf '%s\n' "$$report" | grep -q '| *MISSED *|'; then \
	    printf '%s\n' 'interrogate: undocumented definitions remain; see the MISSED rows above' >&2; \
	    exit 1; \
	  fi

ruff: ## Lint Python sources
	$(UV_ENV) $(UV) tool run ruff@$(RUFF_VERSION) check $(PY_SOURCES)

shellcheck: ## Lint the shell scripts
	shellcheck --shell=bash $(SHELL_SOURCES)

# yamllint reads `.yamllint.yml`, which extends `.yamllint-estate.yml`, and
# resolves that path against the working directory, so it runs from the root.
yamllint: ## Lint every tracked YAML file strictly
	git ls-files -z '*.yml' '*.yaml' | xargs -0 -r $(YAMLLINT) --strict --

actionlint: ## Lint the GitHub Actions workflows, including their shell scripts
	$(ACTIONLINT)

markdownlint: ## Lint Markdown files
	$(MDLINT) "**/*.md" "#.uv-cache" "#.uv-tools" "#.venv"

fmt: ## Format Python sources
	$(UV_ENV) $(UV) tool run ruff@$(RUFF_VERSION) format $(PY_SOURCES)
	$(UV_ENV) $(UV) tool run ruff@$(RUFF_VERSION) check --fix $(PY_SOURCES)
	$(MDTABLEFIX) --in-place $(MDTABLEFIX_SELECT) $(MDTABLEFIX_RULES)
	@unset FORCE_COLOR; $(MDLINT) --fix "**/*.md"

check-fmt: ## Verify Python formatting
	$(UV_ENV) $(UV) tool run ruff@$(RUFF_VERSION) format --check $(PY_SOURCES)
	$(MDTABLEFIX) --check $(MDTABLEFIX_SELECT) $(MDTABLEFIX_RULES)

matrix: ## Print the release build matrix derived from extensions.toml
	python3 scripts/matrix.py extensions.toml

smoke-leg: ## Print the configured pull-request smoke leg
	python3 scripts/matrix.py extensions.toml --smoke-leg

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?##' $(MAKEFILE_LIST) | \
	awk 'BEGIN {FS=":"; printf "Available targets:\n"} {printf "  %-16s %s\n", $$1, $$2}'
