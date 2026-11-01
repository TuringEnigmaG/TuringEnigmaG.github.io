# 《斯泰尔斯庄园奇案》逐章嫌疑分析：句子级别的指控/开脱证据 + 贝叶斯更新
import json
import math
import re
import sys

raw = open(sys.argv[1], encoding="utf-8").read()
start = raw.index("CHAPTER I.\n", raw.index("CHAPTER XIII. POIROT EXPLAINS"))
end = raw.index("*** END OF THE PROJECT GUTENBERG")
body = raw[start:end]
parts = re.split(r"\nCHAPTER [IVXL]+\.\n", "\n" + body)[1:]
assert len(parts) == 13, len(parts)
TITLES = ["I Go to Styles", "The 16th and 17th of July", "The Night of the Tragedy", "Poirot Investigates",
          "“It Isn’t Strychnine, Is It?”", "The Inquest", "Poirot Pays His Debts", "Fresh Suspicions",
          "Dr. Bauerstein", "The Arrest", "The Case for the Prosecution", "The Last Link", "Poirot Explains"]

# 嫌疑人及其所有称呼；Mrs. Inglethorp 是受害人，需要排除
SUSPECTS = {
    "Alfred Inglethorp": r"Alfred Inglethorp|Mr\.? Inglethorp|\bAlfred\b|(?<!Mrs\. )(?<!Mrs )\bInglethorp\b(?!'s mother)",
    "Evelyn Howard": r"Evelyn Howard|Miss Howard|\bEvie\b|\bEvelyn\b",
    "John Cavendish": r"John Cavendish|\bJohn\b",
    "Lawrence Cavendish": r"\bLawrence\b",
    "Mary Cavendish": r"Mary Cavendish|Mrs\.? Cavendish|\bMary\b",
    "Cynthia Murdoch": r"\bCynthia\b",
    "Dr. Bauerstein": r"Bauerstein",
}
CULPRITS = {"Alfred Inglethorp", "Evelyn Howard"}
INCRIM = r"\b(?:poison(?:ed|ing|s)?|strychnine|coffee|cocoa|(?:her|the|a|new|old|last) will|money|quarrel(?:led|s)?|lied|lying|liar|motive|guilty|guilt|suspect(?:ed|s)?|suspicio(?:n|us|ns)|arrest(?:ed)?|chemist|bought|beard|forged|forgery|hid|hidden|hiding|secret(?:ly)?|accus(?:e|ed|ation)|murder(?:ed|er)?|kill(?:ed)?|fragment|despatch-case|key|letter|footprints?|burnt|burned)\b"
CLEAR = r"\b(?:alibi|innocent|impossible|could not have|couldn’t have|couldn't have|cleared|absurd|nonsense|out of the question|not guilty|elsewhere|miles away)\b"

def sentences(text):
    text = re.sub(r"\s+", " ", text)
    return re.split(r"(?<=[.!?”])\s+(?=[A-Z“])", text)

chap_rows = []
for ci, ch in enumerate(parts):
    sents = sentences(ch)
    row = {}
    for s_name, pat in SUSPECTS.items():
        m = inc = clr = 0
        words = {}
        quote = None
        for s in sents:
            if re.search(pat, s):
                m += 1
                hits = [w.lower() for w in re.findall(INCRIM, s, re.I)]
                inc += len(hits)
                clr += len(re.findall(CLEAR, s, re.I))
                for w in hits:
                    words[w] = words.get(w, 0) + 1
                # 记录证据最密集的一句，作为页面上的引文
                if hits and (quote is None or len(hits) > quote[0]) and len(s) < 260:
                    quote = (len(hits), s)
        top = sorted(words.items(), key=lambda kv: -kv[1])[:4]
        row[s_name] = {"mentions": m, "incrim": inc, "clear": clr, "top": top, "quote": quote[1] if quote else None}
    chap_rows.append({"chapter": ci + 1, "title": TITLES[ci], "words": len(ch.split()), "sentences": len(sents), "s": row})

# 贝叶斯更新：每章证据 = (指控 - 1.5×开脱) / sqrt(全章句子数)，对数赔率累加
LAMBDA = 0.6
names = list(SUSPECTS)
logodds = {n: 0.0 for n in names}
for r in chap_rows:
    for n in names:
        f = r["s"][n]
        ev = (f["incrim"] - 1.5 * f["clear"]) / math.sqrt(r["sentences"])
        r["s"][n]["evidence"] = round(ev, 3)
        logodds[n] += LAMBDA * ev
    z = max(logodds.values())
    tot = sum(math.exp(logodds[n] - z) for n in names)
    for n in names:
        r["s"][n]["p"] = round(math.exp(logodds[n] - z) / tot, 4)

for r in chap_rows:
    print(f'{r["chapter"]:>2} {r["title"][:28]:<28}', " ".join(f'{n.split()[-1][:6]}:{r["s"][n]["p"]:.2f}({r["s"][n]["mentions"]})' for n in names))
# 结案前（1-12 章）平均概率：真凶 vs 无辜者
for n in names:
    avg = sum(r["s"][n]["p"] for r in chap_rows[:12]) / 12
    att = sum(r["s"][n]["mentions"] for r in chap_rows[:12])
    print(f'{n:<20} avg p ch1-12 {avg:.3f}  mentions {att}  {"CULPRIT" if n in CULPRITS else ""}')
json.dump({"chapters": chap_rows, "suspects": names, "culprits": sorted(CULPRITS), "lambda": LAMBDA}, open(sys.argv[2], "w"), ensure_ascii=False)
