"""LLM 侦探评测：在每个章节截断点让模型给出各嫌疑人是凶手的概率，并测记忆污染。

三种条件：
  original   原文前 k 章
  renamed    原文前 k 章，但所有嫌疑人改名为中性代号，不提书名和作者（测模型是否靠记忆）
  title_only 只给书名和作者，不给正文（直接测模型是否"背过"答案）

用法：
  .venv/bin/python detective_eval.py --book books/styles.json --dry-run
  .venv/bin/python detective_eval.py --book books/styles.json --models claude-opus-5-5 --conditions title_only
  .venv/bin/python detective_eval.py --book books/styles.json --score
"""
import argparse
import json
import math
import os
import re
import string
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
RUNS = HERE / "runs"

# 每百万 token 的美元价格（输入，输出，缓存读取）；Anthropic 价格来自官方价目表，其余厂商以其官网为准
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}

SYSTEM = (
    "You are a careful detective reading a mystery novel. You will be given the novel's text so far, "
    "a list of suspects, and a question. Using only the text you are given, estimate how likely each "
    "listed suspect is to be the murderer or one of the murderers. Probabilities are over the listed "
    "suspects plus 'someone else', and must sum to 1. Give a one-sentence rationale."
)


# ---------------------------------------------------------------- 书本加载与改名
def load_book(cfg_path):
    cfg = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
    raw = (Path(cfg_path).parent / cfg["source"]).read_text(encoding="utf-8")
    start = raw.index(cfg["start_marker"], raw.index(cfg["start_after"]) if cfg.get("start_after") else 0)
    end = raw.index(cfg["end_marker"]) if cfg.get("end_marker") else len(raw)
    body = raw[start:end]
    if cfg.get("strip_aozora"):
        body = re.sub(r"《[^》]*》|｜|［＃[^］]*］", "", body)   # 青空文库的注音与排版注记
    if cfg.get("segments"):
        # 没有章节的短篇：揭晓之前按段落等分成 N 段，揭晓部分单独作为最后一段
        cut = body.index(cfg["reveal_marker"])
        paras = [p for p in body[:cut].split("\n") if p.strip()]
        n, total = cfg["segments"], sum(len(p) for p in paras)
        chapters, cur, acc = [], [], 0
        for p in paras:
            cur.append(p); acc += len(p)
            if acc >= total * (len(chapters) + 1) / n and len(chapters) < n - 1:
                chapters.append("\n".join(cur)); cur = []
        chapters.append("\n".join(cur))
        chapters.append(body[cut:].strip())
        cfg["reveal_chapter"] = len(chapters)
    else:
        chapters = [c.strip() for c in re.split(cfg["chapter_regex"], "\n" + body) if c.strip()]
    cfg["chapters"] = chapters
    return cfg


def codenames(cfg):
    # 嫌疑人按名单顺序映射为 Suspect A、B、C…
    prefix = cfg.get("codename_prefix", "Suspect ")
    return {name: f"{prefix}{string.ascii_uppercase[i]}" for i, name in enumerate(cfg["suspects"])}


def rename(text, cfg):
    cn = codenames(cfg)
    pairs = [(alias, cn[name]) for name, aliases in cfg["suspects"].items() for alias in aliases]
    pairs += list(cfg.get("other_renames", {}).items())   # 侦探、受害人、地名等能暴露书名的专有名词
    pairs = sorted(pairs, key=lambda p: -len(p[0]))
    cjk = cfg.get("language") in ("ja", "zh")   # 中日文没有词边界，直接整串替换（长名优先）
    for alias, code in pairs:
        text = text.replace(alias, code) if cjk else re.sub(r"(?<![\w])" + re.escape(alias) + r"(?![\w])", code, text)
    return text


# ---------------------------------------------------------------- 提示词与输出格式
def suspect_list(cfg, renamed):
    return list(codenames(cfg).values()) if renamed else list(cfg["suspects"])


def schema(names):
    return {
        "type": "object",
        "properties": {
            "probabilities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"suspect": {"type": "string", "enum": names + ["someone else"]}, "probability": {"type": "number"}},
                    "required": ["suspect", "probability"],
                    "additionalProperties": False,
                },
            },
            "rationale": {"type": "string"},
        },
        "required": ["probabilities", "rationale"],
        "additionalProperties": False,
    }


def build_task(cfg, condition, k):
    """返回 (正文块列表, 问题文字, 嫌疑人名单)。正文块逐章分开，便于跨截断点复用缓存。"""
    renamed = condition == "renamed"
    names = suspect_list(cfg, renamed)
    if condition == "title_only":
        q = (f"Without being shown any text: in the novel \"{cfg['title']}\" by {cfg['author']}, who murdered "
             f"{cfg['victim']}? Suspects: {', '.join(names)}.")
        return [], q, names
    chapters = cfg["chapters"][:k]
    unit = cfg.get("unit", "chapter")
    blocks = [f"[{unit.capitalize()} {i + 1}]\n" + (rename(c, cfg) if renamed else c) for i, c in enumerate(chapters)]
    head = "the story below" if renamed else f"\"{cfg['title']}\" by {cfg['author']}"
    q = (f"You have read {unit}s 1 to {k} of {head}. The victim is {rename(cfg['victim'], cfg) if renamed else cfg['victim']}. "
         f"Suspects: {', '.join(names)}. Based only on these {unit}s, who is the murderer?")
    return blocks, q, names


def normalize(parsed, names):
    probs = {n: 0.0 for n in names + ["someone else"]}
    for item in parsed.get("probabilities", []):
        if item.get("suspect") in probs:
            probs[item["suspect"]] += max(0.0, float(item.get("probability", 0)))
    tot = sum(probs.values())
    return {n: (v / tot if tot > 0 else 1 / len(probs)) for n, v in probs.items()}


# ---------------------------------------------------------------- 各家模型
def call_anthropic(model, blocks, question, names, effort):
    import anthropic

    client = anthropic.Anthropic()
    content = [{"type": "text", "text": b} for b in blocks]
    if content:
        content[-1]["cache_control"] = {"type": "ephemeral"}  # 缓存到最后一章，下一个截断点复用前缀
    content.append({"type": "text", "text": question})
    resp = client.beta.messages.create(
        model=model,
        max_tokens=16000,
        system=SYSTEM,
        messages=[{"role": "user", "content": content}],
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema(names)}},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    fell_back = any(getattr(e, "type", "") == "fallback_message" for e in (resp.usage.iterations or []))
    u = resp.usage
    usage = {"input": u.input_tokens, "output": u.output_tokens,
             "cache_read": u.cache_read_input_tokens or 0, "cache_write": u.cache_creation_input_tokens or 0}
    if resp.stop_reason == "refusal":
        return None, {"status": "refusal", "served_by": resp.model, "fallback": fell_back, "usage": usage}
    text = next((b.text for b in resp.content if b.type == "text"), "")
    status = "fallback_excluded" if fell_back else "ok"   # 换了模型回答的结果不计入该模型成绩
    return json.loads(text), {"status": status, "served_by": resp.model, "fallback": fell_back, "usage": usage}


def call_openai_compatible(model, blocks, question, names, base_url=None, key_env="OPENAI_API_KEY"):
    from openai import OpenAI

    client = OpenAI(base_url=base_url, api_key=os.environ[key_env])
    user = "\n\n".join(blocks + [question, "Reply with JSON only, shaped like: "
                                 + json.dumps({"probabilities": [{"suspect": names[0], "probability": 0.5}], "rationale": "..."})])
    resp = client.chat.completions.create(model=model, messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
                                          response_format={"type": "json_object"})
    u = resp.usage
    return json.loads(resp.choices[0].message.content), {"status": "ok", "served_by": resp.model, "fallback": False,
                                                          "usage": {"input": u.prompt_tokens, "output": u.completion_tokens, "cache_read": 0, "cache_write": 0}}


# 模型名 → (调用函数, 需要的环境变量)
def provider(model):
    if model.startswith("claude-"):
        return (lambda b, q, n, eff: call_anthropic(model, b, q, n, eff)), None
    if model.startswith("deepseek"):
        return (lambda b, q, n, eff: call_openai_compatible(model, b, q, n, "https://api.deepseek.com", "DEEPSEEK_API_KEY")), "DEEPSEEK_API_KEY"
    return (lambda b, q, n, eff: call_openai_compatible(model, b, q, n)), "OPENAI_API_KEY"


# ---------------------------------------------------------------- 运行、缓存结果、评分
def run_file(book_id, model, condition):
    RUNS.mkdir(exist_ok=True)
    return RUNS / f"{book_id}__{model}__{condition}.jsonl"


def done_keys(path):
    if not path.exists():
        return set()
    return {json.loads(l)["cutoff"] for l in path.read_text().splitlines() if l.strip()}


def estimate_tokens(text, lang):
    return len(text) / (1.3 if lang in ("zh", "ja") else 4.0)


def plan(cfg, conditions, cutoffs):
    jobs = []
    for cond in conditions:
        for k in ([0] if cond == "title_only" else cutoffs):
            blocks, q, names = build_task(cfg, cond, k)
            jobs.append((cond, k, blocks, q, names))
    return jobs


def run(args):
    cfg = load_book(args.book)
    n = len(cfg["chapters"])
    cutoffs = list(range(1, cfg["reveal_chapter"])) if args.cutoffs == "all" else [int(x) for x in args.cutoffs.split(",")]
    jobs = plan(cfg, args.conditions.split(","), cutoffs)
    print(f"{cfg['title']}: {n} chapters, cutoffs {cutoffs[0]}–{cutoffs[-1]}, {len(jobs)} prompts per model")
    for model in args.models.split(","):
        fn, key = provider(model)
        if key and not os.environ.get(key):
            print(f"  skip {model}: {key} is not set")
            continue
        tok_in = sum(estimate_tokens("".join(b) + q, cfg["language"]) for _, _, b, q, _ in jobs)
        p = PRICES.get(model)
        out_cost = len(jobs) * 3000 / 1e6 * p[1] if p else 0   # 每次回答约 3k 输出（含思考）
        cost = f"≈ ${tok_in / 1e6 * p[0] * 0.35 + out_cost:.2f} with caching (≈ ${tok_in / 1e6 * p[0] + out_cost:.2f} without)" if p else "price unknown"
        print(f"  {model}: ≈{tok_in / 1e3:,.0f}k input tokens, {cost}")
        if args.dry_run:
            continue
        for cond, k, blocks, q, names in jobs:
            path = run_file(cfg["id"], model, cond)
            if k in done_keys(path):
                continue
            t0 = time.time()
            try:
                parsed, meta = fn(blocks, q, names, args.effort)
            except Exception as e:  # 记录失败，继续下一个截断点
                print(f"    {cond} k={k}: error {type(e).__name__}: {e}")
                continue
            row = {"cutoff": k, "condition": cond, "model": model, "names": names, "seconds": round(time.time() - t0, 1), **meta}
            if parsed is not None:
                row["probs"] = normalize(parsed, names)
                row["rationale"] = parsed.get("rationale", "")
            with path.open("a") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(f"    {cond} k={k}: {meta['status']} · cache read {meta['usage']['cache_read']:,} tokens")


def score(args):
    cfg = load_book(args.book)
    cn = codenames(cfg)
    out = {"book": {k: cfg[k] for k in ("id", "title", "author", "reveal_chapter")}, "suspects": list(cfg["suspects"]), "runs": []}
    for path in sorted(RUNS.glob(f"{cfg['id']}__*.jsonl")):
        rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
        rows = [r for r in rows if r.get("status") == "ok" and "probs" in r]
        if not rows:
            continue
        model, cond = rows[0]["model"], rows[0]["condition"]
        culprits = [cn[c] for c in cfg["culprits"]] if cond == "renamed" else cfg["culprits"]
        back = {v: k for k, v in cn.items()}
        series = []
        for r in sorted(rows, key=lambda r: r["cutoff"]):
            pr = r["probs"]
            ranked = sorted(pr, key=lambda s: -pr[s])
            series.append({
                "cutoff": r["cutoff"],
                "culprit_mass": round(sum(pr.get(c, 0) for c in culprits), 4),
                "top1_correct": ranked[0] in culprits,
                "both_in_top2": set(culprits) <= set(ranked[:2]),
                "probs": {back.get(s, s): round(v, 4) for s, v in pr.items()},  # 改名条件映射回真名，方便页面对照
                "rationale": r.get("rationale", ""),
            })
        first = next((s["cutoff"] for s in series if s["top1_correct"]), None)
        out["runs"].append({"model": model, "condition": cond, "series": series, "first_correct_cutoff": first,
                            "mean_culprit_mass": round(sum(s["culprit_mass"] for s in series) / len(series), 4)})
        print(f"{model:<22} {cond:<10} mean culprit mass {out['runs'][-1]['mean_culprit_mass']:.2f} · first correct at {first}")
    dest = HERE / f"results_{cfg['id']}.json"
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print("wrote", dest)


PROBE_SCHEMA = {
    "type": "object",
    "properties": {"knows_the_book": {"type": "boolean"}, "culprit": {"type": "string"},
                   "confidence": {"type": "number"}, "rationale": {"type": "string"}},
    "required": ["knows_the_book", "culprit", "confidence", "rationale"],
    "additionalProperties": False,
}


def probe(args):
    """只给书名和作者，问凶手是谁；答案由人工对照判分，存进 runs/probe__<model>.jsonl。"""
    import anthropic

    client = anthropic.Anthropic()
    for model in args.models.split(","):
        if not model.startswith("claude-"):
            print(f"  skip {model}: the title probe currently calls Claude only")
            continue
        path = RUNS / f"probe__{model}.jsonl"
        RUNS.mkdir(exist_ok=True)
        done = {json.loads(l)["title"] for l in path.read_text().splitlines()} if path.exists() else set()
        for item in args.probe.split(";"):
            title, author = [x.strip() for x in item.split("|")]
            if title in done:
                continue
            q = (f"Without looking anything up: in the mystery novel {title} by {author}, who is the murderer? "
                 "If you don't actually know this book, set knows_the_book to false and say so rather than guessing. "
                 "Give the culprit's name as it appears in the book, and your confidence from 0 to 1.")
            resp = client.beta.messages.create(
                model=model, max_tokens=16000, messages=[{"role": "user", "content": q}],
                output_config={"effort": args.effort, "format": {"type": "json_schema", "schema": PROBE_SCHEMA}},
                betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            )
            fell_back = any(getattr(e, "type", "") == "fallback_message" for e in (resp.usage.iterations or []))
            row = {"title": title, "author": author, "model": model, "served_by": resp.model, "fallback": fell_back,
                   "stop_reason": resp.stop_reason, "grade": None}   # grade 由人工填写：correct / wrong / abstained
            if resp.stop_reason != "refusal":
                row.update(json.loads(next(b.text for b in resp.content if b.type == "text")))
            with path.open("a") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(f"  {model} · {title}: {row.get('culprit', '(refused)')} (confidence {row.get('confidence')})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--book")
    ap.add_argument("--probe", help='Title-only probe, e.g. "假面山庄|东野圭吾;放学后|东野圭吾"')
    ap.add_argument("--models", default="claude-opus-5-5,claude-sonnet-5-5,claude-haiku-5-5")
    ap.add_argument("--conditions", default="title_only,original,renamed")
    ap.add_argument("--cutoffs", default="all", help='"all" = every chapter before the reveal, or e.g. "2,5,8,12"')
    ap.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--score", action="store_true")
    a = ap.parse_args()
    if a.probe:
        probe(a)
    elif not a.book:
        ap.error("--book is required unless you use --probe")
    else:
        score(a) if a.score else run(a)
