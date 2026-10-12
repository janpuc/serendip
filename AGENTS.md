# serendip

A discover-feed proxy in front of Yattee Server. Start with `docs/BRIEF.md`; it holds the goal,
what is already verified, the open questions and the working process.

- Follow `~/.pi/agent/AGENTS.md`: delegate bounded jobs to subagents (one or two at a time),
  check `usage_status` and pick the lowest model level that fits, ask the `oracle` advisor
  (`openai/gpt-6-astra:high`) on the design before building.
- Work in stages. After each stage, report to Jan what changed and what was verified, then
  wait for the go-ahead.
- Never touch the live cluster or push home-ops. Prepare home-ops changes on a branch and hand
  Jan the diff.
- Never send more than a measured, rate-limited trickle of requests to YouTube from the
  cluster IP. A bot-check error means stop and report.
- Python 3.12, uv, FastAPI, httpx, ruff, pytest. Keep `docs/DESIGN.md` current.
