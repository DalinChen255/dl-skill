"""Phase 3 Step A: 生成数据底稿 + AI 蒸馏任务说明（按 IP定位/运营策略/内容形式三层框架组织）。"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.utils import common

VISUAL_STYLE_SPEC = """## HTML 报告视觉风格规范——"蒸馏简报"

严格按以下规范生成，不得使用灰褐底色+砖红强调色+无圆角无阴影无白卡的工业档案风，
也不得沿用暖米白底+高对比衬线+陶土色点缀这种常见 AI 生成设计默认风格：

- 背景色 `#FAFAF9`（近白）；强调色 `#5546FF`（靠紫，唯一强调色，克制使用）；
  强调色浅调 `#ECE9FF`（用于底色高亮/进度条填充）；正文色 `#16151A`（近黑）；
  次要文字 `#6E6B76`；分割线 `#E5E3E0`
- 标题与大数字字体 Google Fonts `Manrope`（几何无衬线，不用衬线体）；
  中文全部用 `Noto Sans SC`（黑体，按粗细分级，标题 700/正文 400）；
  英文正文与数据标签用 `Inter`
- 模块外观：大圆角 16px + 极轻柔阴影（不用描边，不用无阴影硬边框）的白色浮起卡片
- 首屏：博主核心数据用大字号数字排版呈现（Apple 式的巨大自信数字），不是"大数字+小标签+渐变"模板化写法
- 招牌元素：桌面端加一条侧边"蒸馏进度轨"——把 IP定位/运营策略/内容形式 三层框架做成随滚动
  位置高亮推进的竖向进度指示，兼具导航和"逐层深入蒸馏"的视觉隐喻；移动端可退化为顶部进度条
- 动效（原生 JS，无外部库）：首屏一次性编排好的进场动画（数字滚动计数 + 标题浮现），
  滚动时侧边进度轨跟随高亮；克制，不叠加额外的散点特效（如卡片 hover 描边变色之类）
- 折叠面板用原生 `<details><summary>`；响应式断点 768px，移动端隐藏侧边进度轨
- 技术要求：单文件 HTML，手写 CSS，禁止引入 Tailwind CDN 等外部框架
"""

SKILL_FOLDER_SPEC = """## 创作 Skill 文件夹规范

产出路径：`{blogger_name}_创作指南.skill/SKILL.md`（必须是文件夹，不能是单个 `.skill.md` 文件）

SKILL.md 必须支持两种调用方式，在文档开头明确写出调用说明：

1. **1:1 模仿模式**（默认）：直接照搬本文档记录的目标博主公式与风格创作。
2. **融合风格模式**：用户在触发时指定"保留我自己的哪些维度（如语言习惯/结尾方式/情绪基调），其余维度套用目标博主的公式"。AI 按用户指定的维度做混合，而不是全盘照搬。文档中需要列出可供用户选择混合的维度清单（至少涵盖：标题公式、开头模板、正文结构、情感节奏、语言习惯、CTA 策略、发布节奏建议），每个维度给出目标博主的对应结论，供用户挑选保留/替换。

内容章节需覆盖三层框架的可执行结论（不是原始数据，是给 AI 创作时直接可用的规则）：
- IP 定位（账号搭建示例 / 定位三问结论 / 差异化人设关键词 / 叙事逻辑规则 / 商业模式说明）
- 运营策略（发布节奏建议 / 账号阶段判断）
- 内容形式（标题公式 TOP5 带示例 / 内容四原子拆解规则 / 恒定风格特征清单 / 主题适应性特征说明）
"""


def build_data_digest(blogger_name: str, profile: dict, analysis: dict) -> str:
    lines = [f"# {blogger_name} 数据底稿\n"]
    lines.append("## 账号基础信息")
    lines.append(f"- 昵称：{profile.get('nickname', '')}")
    lines.append(f"- 小红书号：{profile.get('red_id', '')}")
    lines.append(f"- 简介：{profile.get('desc', '')}")
    lines.append(f"- 粉丝数：{profile.get('fans', 0)}")
    lines.append(f"- 获赞与收藏：{profile.get('liked_and_collected', 0)}\n")

    lines.append("## 全量统计")
    lines.append(f"- 分析笔记总数：{analysis['total']}")
    lines.append(f"- 均赞：{analysis['avg_liked']} / 均藏：{analysis['avg_collected']} / 均评：{analysis['avg_comment']}")
    lines.append(f"- 藏赞比：{analysis['collect_like_ratio']}")
    lines.append(f"- 图文 vs 视频：{analysis['image_video_ratio']}")
    lines.append(f"- 标题公式分布：{analysis['title_formula_distribution']}")
    lines.append(f"- 高频标签 TOP20：{analysis['tag_frequency']}")
    lines.append(f"- 发布节奏：{analysis['publish_rhythm']}\n")

    lines.append("## TOP10 爆款笔记")
    for i, note in enumerate(analysis["top10"], start=1):
        lines.append(
            f"{i}. 《{note.get('title', '')}》 赞{note.get('liked_count', 0)} "
            f"藏{note.get('collected_count', 0)} 评{note.get('comment_count', 0)}"
        )
        desc = note.get("desc", "")
        if desc:
            lines.append(f"   正文摘录：{desc[:200]}")
        tags = note.get("tags", [])
        if tags:
            lines.append(f"   标签：{', '.join(tags)}")
    return "\n".join(lines)


def build_ai_task(blogger_name: str) -> str:
    lines = [f"# {blogger_name} AI 蒸馏任务\n"]
    lines.append(
        "请基于同目录下的 `{}_数据底稿.md`，按以下三层框架产出蒸馏结论，"
        "再生成两个最终产出物。每完成一个立即写入磁盘，不等另一个完成。\n".format(blogger_name)
    )

    lines.append("## 第一层：IP 定位分析")
    lines.append("- 账号搭建：拆解昵称、简介、头像的具体设计")
    lines.append("- 账号定位：内容垂直度 + 用户垂直度；回答定位三问——面向什么人群 / 解决什么问题 / 卖什么产品")
    lines.append("- 差异化人设：提炼真实感、反差感、记忆点")
    lines.append("- 叙事逻辑：故事线设计、跨笔记的叙事一致性")
    lines.append(
        "- 商业模式：匹配店铺带货 / 知识付费专栏 / 品牌合作与置换 / 广告 / 无明显变现，"
        "若数据中无相关线索则该项从简，不得编造\n"
    )

    lines.append("## 第二层：运营策略")
    lines.append("- 发布节奏：结合数据底稿的发布节奏统计，给出更新频率与发布时段结论")
    lines.append("- 账号阶段判断：结合发布历史判断起步期/成长期/稳定期，作为节奏合理性的参考依据（非绝对标准）\n")

    lines.append("## 第三层：内容形式")
    lines.append("- 全量统计：基于 TOP10 爆款笔记逐条归类到五类标题公式模板，附具体说明")
    lines.append("- 内容四原子拆解：钩子(Hook) / 情绪锚点(Emotion Anchor) / 认知缺口(Cognitive Gap) / CTA触发器，各自的语言特征和位置规律")
    lines.append(
        "- 代表作精读（计量文体学式双层分析）：从 TOP10 中交叉对比，"
        "分别提炼跨笔记恒定特征（高频用语/句式节奏/人称习惯/签名句式）与"
        "主题适应性特征（不同选题下的风格调整方式）\n"
    )

    lines.append(VISUAL_STYLE_SPEC)
    lines.append(SKILL_FOLDER_SPEC.format(blogger_name=blogger_name))

    lines.append("## 质量红线")
    lines.append(f"- HTML 报告文件名：`{blogger_name}_蒸馏报告.html`，报告正文必须包含"
                  "「IP定位」「运营策略」「内容形式」三个关键词锚点，且三部分均有实质内容")
    lines.append(f"- Skill 文件夹路径：`{blogger_name}_创作指南.skill/SKILL.md`，必须是文件夹，不能是单个文件")
    lines.append("- 生成完毕后运行 `python scripts/utils/quality.py` 对应的校验逻辑（或等价手动检查）确认无遗漏")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="生成数据底稿 + AI 蒸馏任务说明")
    parser.add_argument("analysis_path", help="<user_id>_analysis.json 路径")
    parser.add_argument("blogger_name", help="博主展示名（用于产出物文件名）")
    parser.add_argument("-o", "--output", default="./output", help="输出目录，默认 ./output")
    parser.add_argument("--scan", required=True, help="<user_id>_scan.json 路径（用于读取 profile）")
    args = parser.parse_args()

    analysis = common.load_json(args.analysis_path)
    scan_data = common.load_json(args.scan)
    profile = scan_data.get("profile", {})

    digest = build_data_digest(args.blogger_name, profile, analysis)
    task = build_ai_task(args.blogger_name)

    out_dir = Path(args.output)
    digest_path = out_dir / f"{args.blogger_name}_数据底稿.md"
    task_path = out_dir / f"{args.blogger_name}_AI蒸馏任务.md"
    out_dir.mkdir(parents=True, exist_ok=True)
    digest_path.write_text(digest, encoding="utf-8")
    task_path.write_text(task, encoding="utf-8")

    print(f"✅ 数据底稿：{digest_path}")
    print(f"✅ AI蒸馏任务：{task_path}")


if __name__ == "__main__":
    main()
