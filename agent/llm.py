"""The agent pipeline: guardrail -> route -> analyse -> narrate -> self-review -> output filter.

Two interchangeable back-ends:
  * GeminiBackend  - uses Google Gemini with function calling (the real generative-AI agent)
  * offline router - keyword router + template narrator, used when no API key is set or the API is unavailable,
                     so the demo never dies in front of an assessor.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

from . import guardrails as G
from . import tools as T
from .config import DEFAULT_MODELS
from .knowledge import CRITIC_SYSTEM, NARRATOR_SYSTEM, ROUTER_SYSTEM


@dataclass
class AgentReply:
    text: str
    follow_ups: list[str] = field(default_factory=list)
    results: list[T.ToolResult] = field(default_factory=list)
    mode: str = "offline"                  # gemini:<model> | offline | guardrail
    blocked: bool = False
    critic: dict | None = None
    trace: list[dict] = field(default_factory=list)
    redactions: list[str] = field(default_factory=list)
    notice: str | None = None


# ----------------------------------------------------------------------------------------------- offline back-end
FOLLOW_UPS = {
    "business_summary": ["Which product type is the most profitable?", "What was our biggest production bottleneck last quarter?", "How has our profit changed month by month?"],
    "product_profitability": ["Which products are delivered late most often, and what does it cost us?", "Why are we losing so many quotes? Does price matter?", "How has our profit changed month by month?"],
    "production_bottleneck": ["Show me the impact of employee resignations on our on-time delivery rate.", "What does demand look like for the next 3 months?", "Which products are delivered late most often, and what does it cost us?"],
    "late_delivery_analysis": ["What was our biggest production bottleneck last quarter?", "Show me the impact of employee resignations on our on-time delivery rate.", "Are low-skill designers delivering late more often?"],
    "resignation_impact": ["How is our team made up, and how high is staff turnover?", "What was our biggest production bottleneck last quarter?", "Are low-skill designers delivering late more often?"],
    "workforce_overview": ["Show me the impact of employee resignations on our on-time delivery rate.", "How does wage cost compare with revenue by quarter?", "Which product type is the most profitable?"],
    "inventory_analysis": ["Which product type is the most profitable?", "How has our profit changed month by month?", "What does demand look like for the next 3 months?"],
    "sales_pricing_analysis": ["What does demand look like for the next 3 months?", "Which product type is the most profitable?", "How has our profit changed month by month?"],
    "demand_forecast": ["What was our biggest production bottleneck last quarter?", "Why are we losing so many quotes? Does price matter?", "Which timber runs low most often and what do we spend on it?"],
    "financial_trend": ["Which product type is the most profitable?", "What was our biggest production bottleneck last quarter?", "How is our team made up, and how high is staff turnover?"],
    "late_risk_radar": ["Do we need to hire more makers if demand keeps growing?", "What if we quote longer lead times and demand grows 20%?", "Forecast our quotes for the next 3 months."],
    "forecast_outlook": ["Which orders in progress are most likely to be late?", "What if demand grows 30% - can we cope?", "Is there anything unusual in our monthly numbers?"],
    "scenario_simulator": ["Do we need to hire more makers if demand keeps growing?", "Should we raise our prices? What actually drives winning quotes?", "Forecast our revenue for the next 3 months."],
    "capacity_planner": ["Which orders in progress are most likely to be late?", "What if we raise prices by 10%?", "Forecast our quotes for the next 3 months."],
    "price_strategy_analysis": ["What if we raise prices by 5% and demand stays the same?", "Show me the quote-to-delivery funnel.", "Why did our profit change last quarter?"],
    "profit_bridge": ["Forecast our profit for the next 3 months.", "Which orders in progress are most likely to be late?", "Is there anything unusual in our numbers?"],
    "anomaly_scan": ["Why did our profit change last quarter?", "Forecast our revenue for the next 3 months.", "Which orders in progress are most likely to be late?"],
    "quote_funnel": ["Should we raise our prices?", "Forecast our quotes for the next 3 months.", "Which product type is the most profitable?"],
    "executive_briefing": ["Which orders in progress are most likely to be late?", "What if we raise prices by 10% and demand grows 20%?", "Do we need to hire more makers?"],
    "data_dictionary": ["Which product type is the most profitable?", "What was our biggest production bottleneck last quarter?", "Show me the impact of employee resignations on our on-time delivery rate."],
}

_PRODUCT_WORDS = ("bookshelf", "bookshelves", "book", "dining", "table", "media", "console", "desk", "sideboard")


def _find_product(q: str) -> str | None:
    for w in _PRODUCT_WORDS:
        if re.search(rf"\b{w}", q):
            return T.resolve_product(w)
    return None


def _numbers_near(q: str, words: str) -> float | None:
    m = re.search(rf"(?:{words})\D{{0,25}}?(-?\d+(?:\.\d+)?)\s*%?", q) or re.search(rf"(-?\d+(?:\.\d+)?)\s*%?\s*(?:{words})", q)
    return float(m.group(1)) if m else None


def _pro_route(q: str, period: str | None, has) -> list[tuple[str, dict]] | None:
    if has(r"full review|executive|board|brief me|briefing|everything (i|we) should know|overall picture"):
        return [("executive_briefing", {"period": period or "last_quarter"})]
    if has(r"what if|what happens if|suppose|scenario|simulate|if we (raise|cut|lower|increase|hire|add)"):
        args: dict = {}
        pr = _numbers_near(q, r"price|prices|pricing")
        vol = _numbers_near(q, r"demand|volume|quotes|enquiries|orders")
        mk = _numbers_near(q, r"makers?|hire|hires|staff|headcount")
        tim = _numbers_near(q, r"timber|wood|material")
        if pr is not None:
            args["price_change_pct"] = -abs(pr) if has(r"(cut|lower|reduce|drop|decrease)\w* (our |the )?price") else pr
        if vol is not None:
            args["quote_volume_change_pct"] = -abs(vol) if has(r"(fall|drop|decline|shrink|fewer)") else vol
        if mk is not None:
            args["extra_makers"] = mk
        if tim is not None:
            args["timber_cost_change_pct"] = tim
        return [("scenario_simulator", args)]
    if has(r"hire|hiring|how many makers|more makers|add (a )?maker|capacity plan|can we (cope|handle)|headcount plan"):
        return [("capacity_planner", {})]
    if has(r"at risk|risk|likely to be late|will (we|they) (be )?(late|deliver)|watch ?list|radar"):
        return [("late_risk_radar", {})]
    if has(r"raise (our )?prices?|price rise|elasticity|optimal pric|price sensitiv|drives? (winning|wins)|what drives"):
        return [("price_strategy_analysis", {})]
    if has(r"forecast|predict|projection|next (\d+ )?(months?|quarter)|outlook|what will"):
        metric = ("profit" if has(r"profit|cash") else "quotes" if has(r"quote|demand|enquir") else "win_rate" if has(r"win rate|conversion")
                  else "on_time" if has(r"on.?time") else "costs" if has(r"cost|expense") else "revenue")
        mo = re.search(r"(\d+)[ -]?months?", q)
        return [("forecast_outlook", {"metric": metric, "horizon_months": int(mo.group(1)) if mo else 3})]
    if has(r"why did (our )?(profit|revenue|cash)|what (drove|caused|moved) (profit|revenue)|profit (change|drop|fall|rise|bridge)|explain (the )?profit"):
        return [("profit_bridge", {"period": period or "last_quarter"})]
    if has(r"unusual|anomal|red flag|worry|worrying|outlier|abnormal|something wrong"):
        return [("anomaly_scan", {})]
    if has(r"funnel|conversion|lost value|value (we|of) lost"):
        return [("quote_funnel", {"period": period})]
    return None


def offline_route(question: str) -> list[tuple[str, dict]]:
    q = question.lower()
    period = T.guess_period(question)
    prod = _find_product(q)
    has = lambda pat: re.search(pat, q) is not None
    pro = _pro_route(q, period, has)
    if pro:
        return pro
    if has(r"\b(what does|what is|what's|mean|define|definition|explain|what data|which data|data do you|files?|glossary)\b") and \
            (has(r"\bmean|definition|define|what data|which data|data do you|files?|glossary") or any(k in q for k in T.DEFINITIONS)):
        term = next((k for k in T.DEFINITIONS if k in q), "all")
        return [("data_dictionary", {"topic": term})]
    if has(r"resign|quit|turnover|attrition|hire|hiring|headcount|team|staff|workforce|employee"):
        if has(r"impact|effect|affect|on.?time|late|deliver|performance"):
            return [("resignation_impact", {"period": period})]
        return [("workforce_overview", {"period": period})]
    if has(r"bottleneck|slowest|slow|capacity|constraint|lead time|production time|which stage|stage"):
        return [("production_bottleneck", {"period": period or "last_quarter"})]
    if has(r"\blate\b|on.?time|penalt|delay|deliver"):
        gb = "designer_skill" if has(r"skill|designer") else "quarter" if has(r"quarter|over time|trend") else "month" if has(r"month") \
            else "material" if has(r"material|timber") else "complexity" if has(r"complex") else "product"
        return [("late_delivery_analysis", {"period": period, "group_by": gb})]
    if has(r"forecast|demand|next \d+ months|outlook|predict|season"):
        return [("demand_forecast", {"product": prod})]
    if has(r"win rate|lost|losing|quote|pricing|price|convert|conversion|sales"):
        return [("sales_pricing_analysis", {"period": period, "product": prod})]
    if has(r"profit|margin|earn|worth making|cost per|revenue|financ|cash|wage|expens"):
        if has(r"month|quarter|trend|over time|change|revenue|financ|cash|wage|expens") and not has(r"which product|by product|product type"):
            return [("financial_trend", {"period": period, "granularity": "quarter" if has(r"quarter") else "month"})]
        return [("product_profitability", {"period": period})]
    if has(r"stock|timber|inventory|material|oak|walnut|ash\b|reorder|wood"):
        return [("inventory_analysis", {"period": period, "material": q if has(r"oak|walnut|ash\b") else None})]
    if has(r"summary|overview|how are we|kpi|performance|scorecard|doing|snapshot"):
        return [("business_summary", {"period": period or "last_quarter"})]
    return []


def offline_narrate(results: list[T.ToolResult]) -> tuple[str, list[str]]:
    r = results[0]
    lead = r.facts[0] if r.facts else r.title
    body = "\n".join(f"- {f}" for f in r.facts[1:7])
    text = f"**Bottom line:** {lead}\n\n**What the data shows**\n{body}"
    if r.insights:
        text += "\n" + "\n".join(f"- {i}" for i in r.insights[:4])
    if r.actions:
        text += "\n\n**What I'd do next**\n" + "\n".join(f"- {a}" for a in r.actions[:3])
    caveat = r.confidence or (r.notes[0] if r.notes else "Figures come directly from the simulator data.")
    text += f"\n\nWatch-outs: {caveat}"
    for extra in results[1:]:
        text += f"\n\n**{extra.title}** ({extra.period})\n" + "\n".join(f"- {f}" for f in extra.facts[:4])
    return text, FOLLOW_UPS.get(r.tool, FOLLOW_UPS["business_summary"])


OFFLINE_HELP = ("I couldn't match that to one of my analyses. I can answer questions about production bottlenecks, late deliveries and delivery risk, "
                "staffing and resignations, product profitability, profit drivers, forecasts, what-if scenarios (prices, demand, hiring), pricing strategy, "
                "quote conversion, timber stock, unusual months, and what my metrics mean. Try one of the suggested questions on the home page.")


# ----------------------------------------------------------------------------------------------- Gemini back-end
def _json_loads(text: str) -> dict | None:
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.MULTILINE).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, flags=re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


class GeminiBackend:
    """Thin wrapper around google-genai with model fall-back (handles quota / not-found errors)."""

    _RETRY_HINTS = ("429", "quota", "resource_exhausted", "resource exhausted", "404", "not found", "unavailable", "503", "500",
                    "overloaded", "not supported", "deadline", "timeout")

    def __init__(self, api_key: str, models: list[str] | None = None):
        from google import genai
        from google.genai import types
        self.genai, self.types = genai, types
        self.client = genai.Client(api_key=api_key)
        self.models = list(models or DEFAULT_MODELS)
        self.active: str | None = None

    # -- low level
    def _generate(self, contents, config):
        last: Exception | None = None
        order = ([self.active] if self.active else []) + [m for m in self.models if m != self.active]
        for m in order:
            try:
                resp = self.client.models.generate_content(model=m, contents=contents, config=config)
                self.active = m
                return resp
            except Exception as e:      # noqa: BLE001
                last = e
                if any(h in str(e).lower() for h in self._RETRY_HINTS):
                    continue
                raise
        raise last or RuntimeError("no model available")

    @staticmethod
    def _text(resp) -> str:
        out = []
        for c in (getattr(resp, "candidates", None) or [])[:1]:
            for part in getattr(getattr(c, "content", None), "parts", None) or []:
                if getattr(part, "text", None):
                    out.append(part.text)
        return "".join(out).strip()

    def _schema(self, params: dict):
        t = self.types
        return t.Schema(type="OBJECT", properties={k: t.Schema(type=typ.upper(), description=desc) for k, (typ, desc) in params.items()})

    # -- pipeline steps
    def route(self, question: str, history: list[dict]):
        t = self.types
        decls = [t.FunctionDeclaration(name=n, description=s["desc"], **({"parameters": self._schema(s["params"])} if s["params"] else {})) for n, s in T.REGISTRY.items()]
        contents = [t.Content(role="user" if h["role"] == "user" else "model", parts=[t.Part(text=h["text"][:500])]) for h in history[-6:]]
        contents.append(t.Content(role="user", parts=[t.Part(text=question)]))
        cfg = t.GenerateContentConfig(
            system_instruction=ROUTER_SYSTEM, tools=[t.Tool(function_declarations=decls)], temperature=0.1,
            automatic_function_calling=t.AutomaticFunctionCallingConfig(disable=True))
        resp = self._generate(contents, cfg)
        calls = []
        for c in (getattr(resp, "candidates", None) or [])[:1]:
            for part in getattr(getattr(c, "content", None), "parts", None) or []:
                fc = getattr(part, "function_call", None)
                if fc and fc.name:
                    calls.append((fc.name, dict(fc.args or {})))
        return calls[:4], self._text(resp)

    def narrate(self, question: str, results: list[T.ToolResult], history: list[dict]):
        t = self.types
        ctx = "\n".join(f"{h['role'].upper()}: {h['text'][:300]}" for h in history[-4:])
        prompt = (f"RECENT CONVERSATION:\n{ctx or '(none)'}\n\nQUESTION: {question}\n\nTOOL RESULTS:\n"
                  + "\n\n".join(r.llm_payload() for r in results))
        cfg = t.GenerateContentConfig(system_instruction=NARRATOR_SYSTEM, temperature=0.2, response_mime_type="application/json")
        data = _json_loads(self._text(self._generate(prompt, cfg)))
        if not data or not data.get("answer"):
            raise ValueError("narrator returned no answer")
        return str(data["answer"]), [str(x) for x in (data.get("follow_ups") or [])][:3]

    def critique(self, question: str, results: list[T.ToolResult], draft: str):
        t = self.types
        prompt = (f"QUESTION: {question}\n\nTOOL RESULTS:\n" + "\n\n".join(r.llm_payload() for r in results) + f"\n\nDRAFT ANSWER:\n{draft}")
        cfg = t.GenerateContentConfig(system_instruction=CRITIC_SYSTEM, temperature=0.0, response_mime_type="application/json")
        data = _json_loads(self._text(self._generate(prompt, cfg)))
        if not data:
            raise ValueError("critic returned nothing")
        return data


# ----------------------------------------------------------------------------------------------- orchestration
def _step(trace, name, detail, t0):
    trace.append({"step": name, "detail": detail, "ms": int((time.time() - t0) * 1000)})


def answer(question: str, history: list[dict] | None = None, backend=None, self_review: bool = True) -> AgentReply:
    """Run one question through the whole pipeline. `backend=None` means offline mode."""
    history = history or []
    trace: list[dict] = []
    t0 = time.time()

    # 1. guardrail on the way in
    v = G.check_input(question)
    if not v.allowed:
        _step(trace, "Guardrail check", f"blocked ({v.reason})", t0)
        return AgentReply(text=v.message, mode="guardrail", blocked=True, trace=trace,
                          follow_ups=["Which product type is the most profitable?", "What was our biggest production bottleneck last quarter?"])
    _step(trace, "Guardrail check", "passed", t0)

    notice = None
    mode = "offline"
    results: list[T.ToolResult] = []
    text, follow_ups, critic = "", [], None
    try:
        if backend is not None:
            t1 = time.time()
            calls, plain = backend.route(question, history)
            mode = f"gemini:{backend.active or backend.models[0]}"
            _step(trace, "Understand question (LLM routing)", ", ".join(f"{n}({', '.join(f'{k}={v}' for k, v in a.items())})" for n, a in calls) or "no analysis needed", t1)
            if not calls and plain:
                text, _ = G.sanitize_output(plain)
                return AgentReply(text=text, mode=mode, trace=trace,
                                  follow_ups=["Which product type is the most profitable?", "What was our biggest production bottleneck last quarter?",
                                              "Show me the impact of employee resignations on our on-time delivery rate."])
            if not calls:
                raise ValueError("router returned nothing")
        else:
            calls = offline_route(question)
            _step(trace, "Understand question (keyword routing)", ", ".join(n for n, _ in calls) or "no match", t0)
            if not calls:
                return AgentReply(text=OFFLINE_HELP, mode="offline", trace=trace,
                                  follow_ups=["Which product type is the most profitable?", "What was our biggest production bottleneck last quarter?",
                                              "Show me the impact of employee resignations on our on-time delivery rate."])

        t2 = time.time()
        seen = set()
        for name, args in calls:
            key = (name, json.dumps(args, sort_keys=True, default=str))
            if key in seen:
                continue
            seen.add(key)
            results.append(T.run_tool(name, args))
        _step(trace, "Run vetted analysis", "; ".join(f"{r.tool} on {r.period}" for r in results), t2)

        if backend is not None:
            t3 = time.time()
            text, follow_ups = backend.narrate(question, results, history)
            _step(trace, "Draft answer (LLM)", f"{len(text.split())} words", t3)
            if self_review:
                t4 = time.time()
                try:
                    critic = backend.critique(question, results, text)
                    verdict = str(critic.get("verdict", "pass")).lower()
                    issues = [str(i) for i in (critic.get("issues") or [])]
                    if verdict == "revise" and critic.get("revised_answer"):
                        text = str(critic["revised_answer"])
                    critic = {"verdict": verdict, "issues": issues}
                    _step(trace, "Self-review (critic)", "passed" if verdict == "pass" else f"revised: {'; '.join(issues)[:200]}", t4)
                except Exception as e:      # noqa: BLE001 - a failed review must not fail the answer
                    critic = {"verdict": "skipped", "issues": [f"review unavailable ({type(e).__name__})"]}
                    _step(trace, "Self-review (critic)", "skipped - review service unavailable", t4)
        else:
            text, follow_ups = offline_narrate(results)
            _step(trace, "Compose answer (template)", "offline mode", t0)
    except Exception as e:      # noqa: BLE001 - never leave the user with a stack trace
        if backend is not None:
            notice = "The AI service was busy or unavailable, so this answer used the built-in analysis mode."
            _step(trace, "AI service", f"fell back to offline mode ({type(e).__name__})", t0)
            return answer(question, history, backend=None, self_review=False)._with_notice(notice, trace)
        raise

    # 4. output filter
    t5 = time.time()
    text, red = G.sanitize_output(text)
    for r in results:
        r.table = G.scrub_table(r.table)
    _step(trace, "Output filter", "removed: " + ", ".join(red) if red else "nothing to remove", t5)
    return AgentReply(text=text, follow_ups=follow_ups, results=results, mode=mode, critic=critic, trace=trace, redactions=red, notice=notice)


def _with_notice(self: AgentReply, notice: str, earlier_trace: list[dict]) -> AgentReply:
    self.notice = notice
    self.trace = earlier_trace + self.trace
    return self


AgentReply._with_notice = _with_notice   # type: ignore[attr-defined]
