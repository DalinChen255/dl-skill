"""Phase 4: 产出物质量校验——报告与 Skill 文件夹是否完整、非空。"""

from pathlib import Path

_REQUIRED_KEYWORDS = ["框架", "标题", "开头", "中间", "结尾", "CTA"]


def check_outputs(output_dir: str, name: str) -> list:
    issues = []
    base = Path(output_dir)

    html_path = base / f"{name}_拆解报告.html"
    if not html_path.exists() or html_path.stat().st_size == 0:
        issues.append(f"缺失或为空：{html_path.name}")
    else:
        text = html_path.read_text(encoding="utf-8", errors="ignore")
        missing_keywords = [kw for kw in _REQUIRED_KEYWORDS if kw not in text]
        if missing_keywords:
            issues.append(f"{html_path.name} 缺少必要模块关键词：{', '.join(missing_keywords)}")

    skill_path = base / f"{name}_写作指南.skill"
    if not skill_path.exists():
        issues.append(f"缺失：{skill_path.name}")
    elif not skill_path.is_dir():
        issues.append(f"{skill_path.name} 必须是文件夹，不能是单个文件")
    else:
        skill_md = skill_path / "SKILL.md"
        if not skill_md.exists() or skill_md.stat().st_size == 0:
            issues.append(f"缺失或为空：{skill_path.name}/SKILL.md")

    return issues
