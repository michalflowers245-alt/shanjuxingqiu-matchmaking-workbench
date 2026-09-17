from __future__ import annotations

import re
import statistics
from collections import Counter
from typing import Any, Iterable


FEATURES: dict[str, tuple[str, str]] = {
    "F01": ("堵住所有反驳", "过度替读者预设反对意见，让文章像在完成一场无漏洞答辩。"),
    "F02": ("知识一次塞满", "一篇内容同时交付过多知识点，核心机制容易被稀释。"),
    "F03": ("匀速排比", "连续句子结构和长度过于一致，听感像按模板展开。"),
    "F04": ("重复让步模板", "同一种让步或转折句式反复出现。"),
    "F05": ("概念命名仪式", "为普通判断起新概念，但没有增加解释力。"),
    "F06": ("情绪曲线过于光滑", "情绪按预设台阶整齐升级，缺少真实表达里的停顿和回落。"),
    "F07": ("替读者预设反应", "先替读者说一句话再纠正，容易显得套路化。"),
    "F08": ("整齐反转过密", "“不是 X，而是 Y”一类反转使用过密。"),
    "F09": ("没有任何犹豫", "确定性词语过密，结论强度超过了证据和适用条件。"),
    "F10": ("精确感不真实", "细节非常精确却没有来源，可能让可信度下降。"),
    "F11": ("脆弱感服务论点", "个人脆弱经历出现得过于功能化，只为了给结论盖章。"),
    "F12": ("把结论包装成协议", "把普通结论包装成公式、协议或万能清单，制造不必要的确定感。"),
    "F13": ("段段收束成金句", "自然段频繁以结论式金句结束，节奏太整齐。"),
    "F14": ("句子节奏过匀", "句长差异太小，缺少真人口语的停顿变化。"),
    "F15": ("身体感受代替论证", "用强烈身体感受替代机制、条件或证据。"),
    "F16": ("开头三件套", "开头同时堆钩子、痛点和承诺，容易像批量生成模板。"),
    "F17": ("连接词模板重复", "固定连接词出现太多，暴露了段落拼接痕迹。"),
    "F18": ("同义词刻意轮换", "相邻位置刻意更换近义抽象词，像为了避免重复而改写。"),
    "F19": ("书面或翻译腔", "措辞更像报告或翻译文本，不像真人直接表达。"),
    "F20": ("无法核实的故事入口", "以朋友或客户故事起证据作用，却没有可追溯来源。"),
    "F21": ("祝福式结尾", "结尾用泛化祝福代替具体行动或现实判断。"),
    "F22": ("对深刻感过拟合", "抽象大词密集，但附近缺少场景、条件或证据。"),
}

_CONNECTORS = (
    "所以你会发现",
    "真正的问题",
    "换句话说",
    "这才是关键",
    "值得注意的是",
    "归根结底",
    "从本质上说",
    "基于以上",
)
_ABSTRACT = ("底层逻辑", "认知升级", "时代红利", "本质上", "归根结底", "深层次", "赋能", "闭环")
_BOOKISH = ("针对这一问题", "鉴于此", "基于以上分析", "进行深入", "从而实现", "作为一个", "值得注意的是")
_ENDING_BLESSINGS = ("你值得", "愿你", "希望你能", "相信你一定", "愿我们都", "奔赴更好的")


def _severity(count: int, strong_at: int = 3) -> str:
    return "strong" if count >= strong_at else "medium" if count >= 2 else "weak"


def _location(text: str, start: int) -> dict[str, int]:
    return {
        "start": start,
        "end": start,
        "paragraph": text.count("\n\n", 0, start) + 1,
        "line": text.count("\n", 0, start) + 1,
    }


def _finding(text: str, feature_id: str, start: int, end: int, severity: str, note: str = "") -> dict[str, Any]:
    name, reason = FEATURES[feature_id]
    left, right = max(0, start - 18), min(len(text), end + 24)
    return {
        "feature_id": feature_id,
        "name": name,
        "severity": severity,
        "snippet": text[left:right].strip(),
        **_location(text, start),
        "end": end,
        "reason": note or reason,
        "revision_intent": {
            "F08": "保留最有力的一次反转，其余改成事实、动作或自然停顿。",
            "F09": "把结论强度降到证据能支持的范围，并写清适用条件。",
            "F11": "只有真实且必要时才保留经历；不要让脆弱感替代论证。",
            "F12": "直接说清结论和下一步，不再额外命名一套万能公式。",
            "F15": "把身体感受换成发生了什么、为什么发生和会付出什么成本。",
            "F17": "删掉能直接相接的连接词，只在逻辑真的缺桥时补一句。",
            "F18": "选一个最准确的词自然重复，不必用近义词轮换。",
            "F19": "换成一个人能直接说出口的短句。",
            "F20": "绑定真实来源；没有来源就改成明确的示意场景。",
            "F21": "回到读者眼前的一次选择、代价或下一步。",
            "F22": "用普通场景、条件或可核对证据替换抽象大词。",
        }.get(feature_id, "局部修改这处表达，保留原观点和原有结构。"),
    }


def _iter_matches(text: str, patterns: Iterable[str]) -> list[re.Match[str]]:
    escaped = "|".join(re.escape(item) for item in patterns)
    return list(re.finditer(escaped, text)) if escaped else []


def scan_expression(text: str, content_type: str = "wechat_article") -> dict[str, Any]:
    """Find inspectable expression risks. This is not an AI-authorship detector."""
    body = (text or "").strip()
    if not body:
        return {
            "score": 100,
            "level": "low",
            "findings": [],
            "summary": "没有可检查正文。",
            "disclaimer": _DISCLAIMER,
            "method_version": "expression-22-v1",
            "checked_features": [{"id": key, "name": value[0]} for key, value in FEATURES.items()],
        }

    findings: list[dict[str, Any]] = []

    reverse_matches = list(re.finditer(r"不是[^。！？\n]{1,38}[，,]?而是[^。！？\n]{1,46}", body))
    if len(reverse_matches) >= 2:
        severity = _severity(len(reverse_matches), 3)
        for match in reverse_matches:
            findings.append(_finding(body, "F08", match.start(), match.end(), severity))

    connector_matches = _iter_matches(body, _CONNECTORS)
    connector_counts = Counter(match.group(0) for match in connector_matches)
    repeated_connectors = [match for match in connector_matches if connector_counts[match.group(0)] >= 2]
    if len(connector_matches) >= 3 or repeated_connectors:
        severity = "strong" if len(connector_matches) >= 5 else "medium"
        for match in (repeated_connectors or connector_matches):
            findings.append(_finding(body, "F17", match.start(), match.end(), severity))

    for match in _iter_matches(body, _BOOKISH):
        findings.append(_finding(body, "F19", match.start(), match.end(), "medium"))

    abstract_matches = _iter_matches(body, _ABSTRACT)
    if len(abstract_matches) >= 2:
        severity = "strong" if len(abstract_matches) >= 5 else "medium"
        for match in abstract_matches:
            nearby = body[max(0, match.start() - 45): min(len(body), match.end() + 45)]
            grounded = bool(re.search(r"\d|比如|例如|当时|那天|客户说|资料\s*\[", nearby))
            if not grounded:
                findings.append(_finding(body, "F22", match.start(), match.end(), severity))

    for match in re.finditer(r"(?:我有个朋友|有个朋友|我有个客户|有个客户|前几天有人找我)[^。！？\n]{0,70}", body):
        nearby = body[match.start(): min(len(body), match.end() + 120)]
        if not re.search(r"\[[KRU]\d+\]|来源|经授权|示意", nearby):
            findings.append(_finding(body, "F20", match.start(), match.end(), "strong"))

    for match in re.finditer(r"\d+(?:\.\d+)?(?:秒|分钟|天|人|元|%|％)", body):
        nearby = body[max(0, match.start() - 40): min(len(body), match.end() + 50)]
        if "." in match.group(0) and not re.search(r"\[[KRU]\d+\]|来源|数据显示|统计", nearby):
            findings.append(_finding(body, "F10", match.start(), match.end(), "medium"))

    for match in re.finditer(r"(?:你可能会觉得|你可能会问|有人会说|很多人会说|你一定会想)", body):
        findings.append(_finding(body, "F07", match.start(), match.end(), "weak"))

    rebuttal_matches = list(re.finditer(r"(?:当然|不可否认|也许你会|有人可能|你可能会|这并不意味着|我知道你会说)", body))
    if len(rebuttal_matches) >= 4:
        severity = "strong" if len(rebuttal_matches) >= 7 else "medium"
        for match in rebuttal_matches:
            findings.append(_finding(body, "F01", match.start(), match.end(), severity))

    concession_matches = list(re.finditer(r"(?:虽然[^。！？\n]{1,35}(?:但是|但)|即使[^。！？\n]{1,35}也|哪怕[^。！？\n]{1,35}也)", body))
    if len(concession_matches) >= 2:
        severity = "strong" if len(concession_matches) >= 4 else "medium"
        for match in concession_matches:
            findings.append(_finding(body, "F04", match.start(), match.end(), severity))

    for match in re.finditer(r"(?:这就是所谓的|我们把它叫作|我们称之为|这有一个新名字)", body):
        findings.append(_finding(body, "F05", match.start(), match.end(), "medium"))

    certainty_matches = list(re.finditer(r"(?:一定会|必然会|毫无疑问|注定会|从来都|所有人都|百分之百|绝对不会)", body))
    if len(certainty_matches) >= 2:
        severity = "strong" if len(certainty_matches) >= 4 else "medium"
        for match in certainty_matches:
            nearby = body[max(0, match.start() - 45): min(len(body), match.end() + 45)]
            if not re.search(r"\[[KRU]\d+\]|条件|可能|通常|样本", nearby):
                findings.append(_finding(body, "F09", match.start(), match.end(), severity))

    for match in re.finditer(r"(?:我也曾经|我曾一度|那段时间我|我也崩溃过)[^。！？\n]{0,70}(?:后来我才明白|直到我发现|所以我想告诉你)", body):
        findings.append(_finding(body, "F11", match.start(), match.end(), "medium"))

    for match in re.finditer(r"(?:给你一套|记住这个|我把它总结成)(?:[^。！？\n]{0,20})(?:公式|协议|万能清单|黄金法则|底层框架)", body):
        findings.append(_finding(body, "F12", match.start(), match.end(), "medium"))

    body_matches = list(re.finditer(r"(?:心里一紧|胸口发闷|喘不过气|整夜睡不着|头皮发麻|手心冒汗|浑身发冷)", body))
    if len(body_matches) >= 2:
        severity = "strong" if len(body_matches) >= 4 else "medium"
        for match in body_matches:
            findings.append(_finding(body, "F15", match.start(), match.end(), severity))

    synonym_groups = [
        ("问题", "困境", "难题", "挑战"),
        ("方法", "路径", "方案", "策略"),
        ("改变", "变化", "变革", "转变"),
        ("关键", "核心", "重点", "本质"),
    ]
    for group in synonym_groups:
        matches = _iter_matches(body, group)
        if len({match.group(0) for match in matches}) >= 3 and len(matches) >= 5:
            span = matches[-1].end() - matches[0].start()
            if span <= 320:
                findings.append(_finding(body, "F18", matches[0].start(), matches[-1].end(), "weak"))

    paragraphs = [item.strip() for item in re.split(r"\n\s*\n", body) if item.strip()]
    paragraph_offsets: list[int] = []
    cursor = 0
    for paragraph in paragraphs:
        offset = body.find(paragraph, cursor)
        paragraph_offsets.append(max(0, offset))
        cursor = max(0, offset) + len(paragraph)

    if content_type != "sales_reply" and len(paragraphs) >= 8:
        enumeration = list(re.finditer(r"(?:首先|其次|再次|另外|与此同时|最后|第一|第二|第三|第四|第五)", body))
        if len(enumeration) >= 6:
            findings.append(_finding(body, "F02", enumeration[0].start(), enumeration[-1].end(), "medium"))

    emotion_words = ("好奇", "担心", "焦虑", "害怕", "崩溃", "绝望", "释然", "重获希望")
    emotion_hits = _iter_matches(body, emotion_words)
    emotion_order = [emotion_words.index(match.group(0)) for match in emotion_hits]
    if len(emotion_order) >= 5 and all(a <= b for a, b in zip(emotion_order, emotion_order[1:])):
        findings.append(_finding(body, "F06", emotion_hits[0].start(), emotion_hits[-1].end(), "weak"))

    if content_type != "short_video" and len(paragraphs) >= 5:
        neat = []
        for index, paragraph in enumerate(paragraphs):
            last = re.split(r"[。！？]", paragraph.rstrip("。！？"))[-1]
            if re.search(r"(?:才是|就是|决定了|从来不是|终究是|最重要的)$", last) or re.match(r"^(真正|最终|归根结底)", last):
                neat.append(index)
        if len(neat) / len(paragraphs) >= 0.45:
            for index in neat:
                start = paragraph_offsets[index] + max(0, len(paragraphs[index]) - 36)
                findings.append(_finding(body, "F13", start, paragraph_offsets[index] + len(paragraphs[index]), "medium"))

    sentences = [s.strip() for s in re.split(r"[。！？\n]+", body) if len(s.strip()) >= 4]
    if content_type not in {"moments", "sales_reply", "community"} and len(sentences) >= 8:
        lengths = [len(s) for s in sentences]
        mean = statistics.fmean(lengths)
        cv = statistics.pstdev(lengths) / mean if mean else 1
        if cv < 0.22:
            findings.append(_finding(body, "F14", 0, min(len(body), 60), "medium", f"全稿句长变异系数仅 {cv:.2f}，节奏过于平均。"))

        windows = [lengths[i:i + 3] for i in range(0, len(lengths) - 2)]
        if any(max(window) - min(window) <= 3 for window in windows):
            findings.append(_finding(body, "F03", 0, min(len(body), 80), "weak"))

    opening = body[:180]
    if content_type in {"short_video", "ad", "xiaohongshu"}:
        has_hook = bool(re.search(r"你|很多人|如果|别急|为什么|千万|注意", opening))
        has_pain = bool(re.search(r"焦虑|痛苦|吃亏|失败|没用|困住|问题|风险", opening))
        has_promise = bool(re.search(r"接下来|看完|教你|告诉你|记住这|三个|\d+个", opening))
        if has_hook and has_pain and has_promise:
            findings.append(_finding(body, "F16", 0, min(len(body), 180), "medium"))

    tail_start = max(0, len(body) - 180)
    for phrase in _ENDING_BLESSINGS:
        start = body.find(phrase, tail_start)
        if start >= 0:
            findings.append(_finding(body, "F21", start, start + len(phrase), "medium"))

    # Keep one evidence-backed finding per feature/location and preserve reading order.
    unique: dict[tuple[str, int], dict[str, Any]] = {}
    for item in findings:
        key = (item["feature_id"], int(item["start"]))
        unique[key] = item
    ordered = sorted(unique.values(), key=lambda item: (int(item["start"]), item["feature_id"]))
    penalty = sum({"strong": 10, "medium": 5, "weak": 2}[item["severity"]] for item in ordered)
    score = max(0, min(100, 100 - penalty))
    strong = sum(item["severity"] == "strong" for item in ordered)
    medium = sum(item["severity"] == "medium" for item in ordered)
    level = "high" if strong >= 2 or score < 60 else "medium" if strong or medium >= 2 else "low"
    summary = (
        "没有发现明显模板化表达，可以保留当前语言。"
        if not ordered
        else f"共定位 {len(ordered)} 处表达风险，其中强风险 {strong} 处、中风险 {medium} 处。只建议局部修改，不推翻原稿。"
    )
    return {
        "score": score,
        "level": level,
        "findings": ordered,
        "summary": summary,
        "disclaimer": _DISCLAIMER,
        "method_version": "expression-22-v1",
        "checked_features": [{"id": key, "name": value[0]} for key, value in FEATURES.items()],
    }


def build_voice_dna(samples: list[str], declared_voice: str = "") -> dict[str, Any]:
    cleaned = [item.strip() for item in samples if item and item.strip()]
    combined = "\n".join(cleaned)
    sentences = [s.strip() for s in re.split(r"[。！？\n]+", combined) if s.strip()]
    lengths = [len(s) for s in sentences]
    avg = round(statistics.fmean(lengths), 1) if lengths else 0
    short_ratio = round(sum(length <= 18 for length in lengths) / len(lengths), 2) if lengths else 0
    pronouns = Counter(re.findall(r"我|我们|你|你们|大家|他|她|他们", combined))
    frequent = [word for word, count in Counter(re.findall(r"[\u4e00-\u9fff]{2,6}", combined)).most_common(30) if count >= 2]
    expression = scan_expression(combined, "wechat_article") if combined else {"findings": []}
    avoid = list(dict.fromkeys(item["snippet"] for item in expression["findings"] if item["severity"] == "strong"))[:6]
    return {
        "source_count": len(cleaned),
        "sample_characters": len(combined),
        "declared_voice": declared_voice,
        "sentence_rhythm": {"average_length": avg, "short_sentence_ratio": short_ratio, "variation": "明显" if lengths and statistics.pstdev(lengths) >= 8 else "温和"},
        "preferred_pronouns": [word for word, _ in pronouns.most_common(3)],
        "repeated_expressions": frequent[:8],
        "avoid_patterns": avoid,
        "instructions": [
            "保留样本中的长短句比例和第一人称习惯，不复制样本原句。",
            "只学习表达习惯，不继承样本里的事实、案例或观点。",
            "优先写具体动作、场景和判断；不为追求顺滑补空泛连接词。",
        ],
        "ready": len(combined) >= 120,
    }


def numeric_tokens(text: str) -> set[str]:
    """Return numeric claims while ignoring ordinary list and section numbering."""
    value = text or ""
    tokens: set[str] = set()
    pattern = re.compile(r"\d+(?:\.\d+)?(?:%|％|元|万|亿|人|天|年|小时|分钟|秒)?")
    for match in pattern.finditer(value):
        token = match.group(0)
        line_start = value.rfind("\n", 0, match.start()) + 1
        before = value[line_start:match.start()]
        after = value[match.end():match.end() + 3]
        # “1. …”“2、…” and “第1天 …” describe document structure rather
        # than a factual claim.  Large bare numbers such as “985” remain checked.
        list_prefix = not before.strip() or bool(re.fullmatch(r"\s*[-*•（(]?\s*", before))
        if list_prefix and re.match(r"\s*[.)） 、:：]", after):
            continue
        if before.rstrip().endswith("第") and any(token.endswith(unit) for unit in ("天", "篇", "节", "步")):
            continue
        tokens.add(token)
    return tokens


def citation_tokens(text: str) -> set[str]:
    return set(re.findall(r"\b[BKRU]\d+\b", text or ""))


def new_unsupported_tokens(before: str, after: str, valid_source_ids: set[str]) -> list[str]:
    issues: list[str] = []
    extra_numbers = sorted(numeric_tokens(after) - numeric_tokens(before))
    if extra_numbers:
        issues.append("真人化改写新增了原稿没有的数字：" + "、".join(extra_numbers[:8]))
    extra_citations = sorted(citation_tokens(after) - citation_tokens(before) - valid_source_ids)
    if extra_citations:
        issues.append("真人化改写新增了不存在的来源编号：" + "、".join(extra_citations))
    return issues


_DISCLAIMER = "这是表达痕迹与真人口吻质检，不判断作者身份，也不把“更像真人”等同于内容质量。"
