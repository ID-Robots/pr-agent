import os

# Price calls from the cost map bundled with LiteLLM, as the Docker images do: the numbers stay
# reproducible and importing LiteLLM fetches nothing. Must be set before pr_agent is imported.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

from tools.review_eval.cli import main  # noqa: E402

raise SystemExit(main())
