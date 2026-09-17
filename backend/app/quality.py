from __future__ import annotations

import difflib
import re
from itertools import combinations
from typing import Any

from .authenticity import citation_tokens, scan_expression
from .prompts import CONTENT_TYPES


GENERIC_OPENINGS = (
    "随着时代的发展",
    "在当今社会",
    "在这个快速发展的时代",
    "众所周知",
    "你是否也曾",
)


def _plain(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", text or "").lower()


def _check(key: str, label: str, status: str, detail: str) -> dict[str, str]:
    return {"key": key, "label": label, "status": status, "detail": detail}


def _source_overlap(body: str, sources: list[dict[str, Any]]) -> dict[str, Any]:
    normalized = _plain(body)
    best = {"source_id": "", "title": "", "ratio": 0.0, "longest_run": 0}
    if not normalized:
        return best
    for source in sources:
        excerpt = _plain(str(source.get("excerpt") or ""))
        if len(excerpt) < 40:
            continue
        matcher = difflib.SequenceMatcher(None, normalized[:12000], excerpt[:5000], autojunk=False)
        match = matcher.find_longest_match(0, min(len(normalized), 12000), 0, min(len(excerpt), 5000))
        ratio = match.size / max(1, min(len(normalized), len(excerpt)))
        if match.size > best["longest_run"] or ratio > best["ratio"]:
            best = {
                "source_id": str(source.get("source_key") or source.get("id") or ""),
                "title": str(source.get("title") or ""),
                "ratio": round(ratio, 4),
                "longest_run": int(match.size),
            }
    return best


def assess_draft(
    package: dict[str, Any],
    *,
    content_type: str,
    platform: str,
    valid_source_ids: set[str],
    strategy: dict[str, Any] | None = None,
    sources: list[dict[str, Any]] | None = None,
    constraints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Deterministic delivery checks. This complements, rather than replaces, editor judgment."""
    spec = CONTENT_TYPES[content_type]
    body = str(package.get("body") or "").strip()
    title = str(package.get("title") or "").strip()
    cta = str(package.get("cta") or "").strip()
    constraints = constraints or {}
    strategy = strategy or {}
    sources = sources or []
    product_mode = str(constraints.get("product_mode") or "")
    char_count = len(re.sub(r"\s+", "", body))
    expected = constraints.get("target_length")
    if isinstance(expected, (list, tuple)) and len(expected) == 2:
        lower, upper = int(expected[0]), int(expected[1])
    else:
        lower, upper = (int(item) for item in spec["length"])

    blockers: list[str] = []
    warnings: list[str] = []
    instructions: list[str] = []
    checks: list[dict[str, str]] = []

    if lower <= char_count <= upper:
        checks.append(_check("length", "体裁长度", "pass", f"正文 {char_count} 字，位于建议区间 {lower}—{upper} 字。"))
    else:
        detail = f"正文 {char_count} 字，建议区间为 {lower}—{upper} 字。"
        hard_short = char_count < max(40, int(lower * 0.4))
        hard_long = char_count > int(upper * 1.6)
        if hard_short or hard_long:
            blockers.append(f"体裁长度严重偏离：{detail}")
            instructions.append("正文长度｜保留核心机制和真实材料｜按每段新增的信息补足或删减｜避免靠重复观点凑字数")
            checks.append(_check("length", "体裁长度", "fail", detail))
        else:
            warnings.append(detail)
            checks.append(_check("length", "体裁长度", "warn", detail))

    if not title:
        blockers.append("标题为空，无法形成可交付文案。")
        checks.append(_check("title", "标题", "fail", "没有主标题。"))
    else:
        checks.append(_check("title", "标题", "pass", f"主标题 {len(title)} 字。"))

    opening_hit = next((item for item in GENERIC_OPENINGS if item in body[:100]), "")
    if opening_hit:
        blockers.append(f"开头使用空泛起手式：“{opening_hit}”。")
        instructions.append(f"开头｜保留主题判断｜删掉“{opening_hit}”并从拆题卡的具体场景开口｜让受众第一句就认出自己的处境")
        checks.append(_check("opening", "开头切口", "fail", f"命中空泛起手式“{opening_hit}”。"))
    else:
        checks.append(_check("opening", "开头切口", "pass", "未使用常见背景铺垫。"))

    if not cta and product_mode != "moments_life_case":
        blockers.append("没有唯一 CTA，文案无法完成任务目标。")
        instructions.append("结尾｜保留现实判断｜补一个与目标一致的低门槛动作｜只留一个行动")
        checks.append(_check("cta", "行动承接", "fail", "CTA 为空。"))
    elif cta:
        checks.append(_check("cta", "行动承接", "pass", f"已设置唯一 CTA：“{cta[:48]}”。"))
    else:
        checks.append(_check("cta", "行动承接", "pass", "生活记录不强制添加营销行动。"))

    deliverables = package.get("deliverables") if isinstance(package.get("deliverables"), dict) else {}
    if product_mode == "moments_life_case":
        raw_variants = deliverables.get("life_case_variants") if isinstance(deliverables.get("life_case_variants"), list) else []
        life_variants = [item for item in raw_variants if isinstance(item, dict)]
        expected_angles = {"daily", "reflection", "soft_business"}
        angles = {str(item.get("angle") or "") for item in life_variants}
        recommended = [item for item in life_variants if bool(item.get("recommended"))]
        invalid_bodies: list[str] = []
        placeholder_bodies: list[str] = []
        generic_openings: list[str] = []
        templated_bodies: list[str] = []
        internally_repeated: list[str] = []
        exposed_source_ids: list[str] = []
        normalized_bodies: list[str] = []
        for item in life_variants:
            label = str(item.get("label") or item.get("angle") or "未命名角度")
            variant_body = str(item.get("body") or "").strip()
            length = len(re.sub(r"\s+", "", variant_body))
            if not 80 <= length <= 500:
                invalid_bodies.append(f"{label}（{length}字）")
            if re.search(r"待补|待填写|TODO|XXX|\[请.{0,8}\]", variant_body, flags=re.I):
                placeholder_bodies.append(label)
            if re.search(r"\[(?:L|P)\d+\]", variant_body, flags=re.I):
                exposed_source_ids.append(label)
            if any(opening in variant_body[:100] for opening in GENERIC_OPENINGS):
                generic_openings.append(label)
            expression = scan_expression(variant_body, "moments")
            if len([finding for finding in expression["findings"] if finding["severity"] == "strong"]) >= 3:
                templated_bodies.append(label)
            paragraphs = [_plain(part) for part in re.split(r"\n\s*\n", variant_body) if len(_plain(part)) >= 24]
            if any(
                difflib.SequenceMatcher(None, left, right, autojunk=False).ratio() >= 0.82
                for left, right in combinations(paragraphs, 2)
            ):
                internally_repeated.append(label)
            if variant_body:
                normalized_bodies.append(_plain(variant_body))
        repeated_pairs = sum(
            1
            for left, right in combinations(normalized_bodies, 2)
            if difflib.SequenceMatcher(None, left, right, autojunk=False).ratio() >= 0.82
        )
        issues: list[str] = []
        if len(life_variants) != 3 or angles != expected_angles:
            issues.append("必须恰好包含真实日常、有感而发、轻度业务启发三个角度")
        if len(recommended) != 1:
            issues.append("必须且只能推荐一版")
        if invalid_bodies:
            issues.append("每版应为80—500字完整正文：" + "、".join(invalid_bodies))
        if placeholder_bodies:
            issues.append("不能含待补占位语：" + "、".join(placeholder_bodies))
        if generic_openings:
            issues.append("不能使用空泛起手式：" + "、".join(generic_openings))
        if templated_bodies:
            issues.append("仍有多处强风险模板化表达：" + "、".join(templated_bodies))
        if internally_repeated:
            issues.append("单篇内部存在换词重复：" + "、".join(internally_repeated))
        if exposed_source_ids:
            issues.append("可发布正文不能露出内部来源编号：" + "、".join(exposed_source_ids))
        if repeated_pairs:
            issues.append("三版角度高度相似，不能只做换词改写")
        if recommended and body != str(recommended[0].get("body") or "").strip():
            issues.append("主正文必须与主编推荐版一致")
        if issues:
            blockers.append("生活案例朋友圈交付不完整：" + "；".join(issues) + "。")
            instructions.append("三版朋友圈｜只保留图片与用户文字能够支持的细节｜分别完成真实日常、有感而发、轻度业务启发三篇独立正文并只推荐一版｜不得补造人物关系、地点、时间、情绪或产品结果")
            checks.append(_check("life_case_variants", "生活案例三版交付", "fail", "；".join(issues)))
        else:
            checks.append(_check("life_case_variants", "生活案例三版交付", "pass", "三个不同角度均为完整正文，且已明确主编推荐版。"))
    elif product_mode == "moments_ops":
        expected_days = max(1, min(14, int(constraints.get("campaign_days") or 7)))
        moments_posts = deliverables.get("moments_posts") if isinstance(deliverables.get("moments_posts"), list) else []
        valid_posts = [item for item in moments_posts if isinstance(item, dict) and str(item.get("content") or "").strip()]
        delivered_days = {
            int(item.get("day")) for item in valid_posts
            if str(item.get("day") or "").isdigit()
        }
        missing_days = [day for day in range(1, expected_days + 1) if day not in delivered_days]
        incomplete_posts = [
            item for item in valid_posts
            if not all(str(item.get(key) or "").strip() for key in ("time", "role", "purpose"))
        ]
        if missing_days or incomplete_posts:
            details: list[str] = []
            if missing_days:
                details.append(f"缺少第{'、'.join(str(day) for day in missing_days)}天")
            if incomplete_posts:
                details.append(f"{len(incomplete_posts)}条缺少发布时间、内容角色或发布目的")
            blockers.append(f"朋友圈运营计划不完整：{'；'.join(details)}。")
            instructions.append("朋友圈周期｜保留已经完成的日期｜补齐每个缺失日期的发布时间、内容角色、完整正文和互动承接｜不得只补标题或提纲")
            checks.append(_check("moments_campaign", "朋友圈运营周期", "fail", f"应交付 {expected_days} 天，当前有效结构化内容 {len(valid_posts)} 条。"))
        else:
            checks.append(_check("moments_campaign", "朋友圈运营周期", "pass", f"已交付连续 {expected_days} 天结构化内容。"))
    elif product_mode == "xiaohongshu_ip":
        publish = deliverables.get("xiaohongshu_publish") if isinstance(deliverables.get("xiaohongshu_publish"), dict) else {}
        publish_title = str(publish.get("title") or "").strip()
        publish_body = str(publish.get("body") or "").strip()
        publish_tags = publish.get("tags") if isinstance(publish.get("tags"), list) else []
        missing: list[str] = []
        if not publish_title:
            missing.append("发布标题")
        if not publish_body:
            missing.append("发布正文")
        if not 5 <= len(publish_tags) <= 10:
            missing.append("至少5个自然标签")
        if missing:
            blockers.append(f"小红书交付包不完整：{'、'.join(missing)}。")
            instructions.append("小红书交付｜保留正文和真实 IP 信息｜补齐可一键复制的发布版与5—10个自然标签｜不得添加虚假身份或结果")
            checks.append(_check("xiaohongshu_package", "小红书发布包", "fail", "、".join(missing)))
        else:
            checks.append(_check("xiaohongshu_package", "小红书发布包", "pass", f"发布版完整，包含 {len(publish_tags)} 个标签。"))
    elif product_mode == "douyin_emotion":
        douyin = deliverables.get("douyin_script") if isinstance(deliverables.get("douyin_script"), dict) else {}
        hook = str(douyin.get("hook") or "").strip()
        script = str(douyin.get("script") or "").strip()
        beats = douyin.get("emotion_beats") if isinstance(douyin.get("emotion_beats"), list) else []
        ending = str(douyin.get("ending") or "").strip()
        opening = re.sub(r"\s+", "", body[:90])
        missing: list[str] = []
        if not hook or len(re.sub(r"\s+", "", hook)) < 12 or len(opening) < 35:
            missing.append("前3秒关系或情绪现场")
        if not script:
            missing.append("完整口播正文")
        if len(beats) < 3:
            missing.append("至少3个情绪推进节点")
        if not ending:
            missing.append("自然收束")
        if missing:
            blockers.append(f"抖音情感稿结构不完整：{'、'.join(missing)}。")
            instructions.append("开头前3秒｜保留主题情绪｜从一个动作、对话或关系瞬间直接进入｜不要补背景介绍或伤感金句")
            checks.append(_check("douyin_opening", "抖音情绪口播", "fail", "、".join(missing)))
        else:
            checks.append(_check("douyin_opening", "抖音情绪口播", "pass", f"前3秒、正文、{len(beats)}个情绪节点和收束均完整。"))

    conversion = package.get("conversion_structure") if isinstance(package.get("conversion_structure"), dict) else {}
    missing_conversion = [key for key in ("hook", "trust", "proof", "objection", "action") if not str(conversion.get(key) or "").strip()]
    if len(missing_conversion) >= 3:
        warnings.append(f"转化结构仍缺：{'、'.join(missing_conversion)}。")
        checks.append(_check("conversion", "转化结构", "warn", f"缺少 {'、'.join(missing_conversion)}。"))
    else:
        checks.append(_check("conversion", "转化结构", "pass", "钩子、信任、证据、异议和行动已形成可检查结构。"))

    claims = package.get("claims") if isinstance(package.get("claims"), list) else []
    invalid_claims: list[str] = []
    pending_claims: list[str] = []
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        ids = {str(item) for item in (claim.get("evidence_ids") or [])}
        status = str(claim.get("status") or "pending")
        if status == "verified" and (not ids or ids - valid_source_ids):
            invalid_claims.append(str(claim.get("claim") or "未命名卖点"))
        elif status != "verified":
            pending_claims.append(str(claim.get("claim") or "未命名卖点"))
    evidence_body = body
    if product_mode == "moments_life_case":
        rows = deliverables.get("life_case_variants") if isinstance(deliverables.get("life_case_variants"), list) else []
        evidence_body = "\n".join(
            str(item.get("body") or "") for item in rows if isinstance(item, dict)
        )
    invalid_body_ids = citation_tokens(evidence_body) - valid_source_ids
    if invalid_claims or invalid_body_ids:
        detail = "；".join([
            *(f"无有效来源的已核实主张：{item}" for item in invalid_claims[:3]),
            *(f"正文引用不存在的来源 [{item}]" for item in sorted(invalid_body_ids)),
        ])
        blockers.append(detail)
        instructions.append("证据位置｜保留能够核对的判断｜绑定真实来源或明确改成待核实｜不得补造案例和编号")
        checks.append(_check("evidence", "证据绑定", "fail", detail))
    else:
        detail = f"有效来源 {len(valid_source_ids)} 条；待核实主张 {len(pending_claims)} 条。"
        if pending_claims:
            warnings.append(f"仍有 {len(pending_claims)} 条主张标记为待核实，发布前需要作者确认。")
            checks.append(_check("evidence", "证据绑定", "warn", detail))
        else:
            checks.append(_check("evidence", "证据绑定", "pass", detail))

    paragraphs = [_plain(item) for item in re.split(r"\n\s*\n", body) if len(_plain(item)) >= 24]
    repeated: list[tuple[int, int]] = []
    for (left_index, left), (right_index, right) in combinations(enumerate(paragraphs, 1), 2):
        ratio = difflib.SequenceMatcher(None, left, right, autojunk=False).ratio()
        if ratio >= 0.82:
            repeated.append((left_index, right_index))
    if repeated:
        pairs = "、".join(f"第{left}/{right}段" for left, right in repeated[:3])
        blockers.append(f"段落疑似换词重复：{pairs}。")
        instructions.append(f"{pairs}｜保留信息更具体的一段｜另一段必须增加新的因果、证据或决策｜不能只换同义词")
        checks.append(_check("progression", "段落推进", "fail", f"发现 {len(repeated)} 组高度相似段落。"))
    else:
        checks.append(_check("progression", "段落推进", "pass", "没有发现高度相似的段落复写。"))

    comparison_sources = [
        source for source in sources
        if str(source.get("kind") or "") not in {"life_case_text", "life_case_image"}
    ]
    source_overlap = _source_overlap(body, comparison_sources)
    copied = source_overlap["longest_run"] >= 140 or (source_overlap["longest_run"] >= 80 and source_overlap["ratio"] >= 0.45)
    if copied:
        blockers.append(
            f"与来源 {source_overlap['source_id'] or source_overlap['title']} 存在过长连续相同文本（{source_overlap['longest_run']} 字），不属于抽象结构借鉴。"
        )
        instructions.append("相似段落｜保留可引用事实并标来源｜用当前品牌自己的判断和论证顺序重写｜不得逐句换词")
        checks.append(_check("originality", "来源相似度", "fail", f"最长连续相同 {source_overlap['longest_run']} 字。"))
    else:
        checks.append(_check("originality", "来源相似度", "pass", f"最长连续相同 {source_overlap['longest_run']} 字。"))

    blueprint_ready = bool(strategy.get("controlling_idea") and strategy.get("argument_beats"))
    if blueprint_ready:
        checks.append(_check("blueprint", "拆题执行", "pass", "核心机制和段落任务均可追溯。"))
    else:
        warnings.append("拆题卡缺少核心机制或段落推进，主编需要人工确认正文没有空转。")
        checks.append(_check("blueprint", "拆题执行", "warn", "核心机制或段落任务不完整。"))

    penalty = len(blockers) * 18 + len(warnings) * 5
    score = max(0, 100 - penalty)
    return {
        "score": score,
        "passed": not blockers,
        "character_count": char_count,
        "expected_range": [lower, upper],
        "platform": platform,
        "content_type": content_type,
        "checks": checks,
        "blockers": list(dict.fromkeys(blockers)),
        "warnings": list(dict.fromkeys(warnings)),
        "revision_instructions": list(dict.fromkeys(instructions)),
        "source_overlap": source_overlap,
        "summary": "成稿硬校验已通过。" if not blockers else f"成稿硬校验发现 {len(blockers)} 个阻断项，不能直接定稿。",
        "method_version": "copy-delivery-v2",
    }
