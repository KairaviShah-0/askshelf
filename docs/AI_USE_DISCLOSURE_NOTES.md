# AI-use disclosure - working notes (DRAFT for you to check and rewrite in your own words)

The brief lets you use AI coding tools to build the agent, but requires you to disclose key prompts and logical help, and to credit
design patterns. The slide content and voice-over must be your own. This file is a factual record of what was AI-assisted, to help you
write your own disclosure. Check every line is accurate before using it.

## Tools used
* Claude (Anthropic, via Claude Code) - designed the agent architecture, wrote the app code, tests and README.
* Google Gemini (via the Gemini API) - the model that runs inside the finished agent to route questions, draft answers and review them.

## Your key prompts to the coding assistant (paraphrased from the chat - replace with your exact wording)
1. Shared the assessment brief and your notes: MVP/phase-2 plan, who monitors data pipelines, add a feedback module, domain-knowledge gap
   in AI tools, ask the agent to criticise itself to catch leakage, never leak customer IDs/important details (reply "contact the manager"),
   make it answer any question, suggested questions on the home page, good UI.
2. "Let's first decide which agent I want to make" (topic at HD level) -> chose Option C: executive analytics copilot.
3. "Let's make a web app... I have to submit a link... suggest what's best" -> Streamlit + Streamlit Community Cloud + Gemini API.
4. "We would be using VS Code for this."

## Prompts and logic inside the agent (these are key prompts too)
* Router, narrator and critic system prompts: `agent/knowledge.py`
* Privacy rules (input filter, output filter): `agent/guardrails.py`
* Data privacy allow-lists: `agent/data.py`
* Analysis tools (all business calculations): `agent/tools.py`

## Design patterns to credit (add proper RMIT Harvard references yourself)
* LLM function-calling / tool-use ("semantic layer" of vetted tools instead of LLM-written code)
* LLM-as-critic / self-review pass
* Layered guardrails (input filter, data minimisation, output filter)
* Human feedback loop
